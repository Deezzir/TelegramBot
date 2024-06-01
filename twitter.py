import aiohttp
import asyncio
import time
import requests
import re
import logging
import utils
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from aiogram.methods import SendMessage
from aiogram import Bot
from aiogram.types import LinkPreviewOptions
from aiogram.exceptions import TelegramAPIError
from os import getenv
from dotenv import load_dotenv
from typing import Dict, Optional
from db import MongoDB

load_dotenv()

INTERVAL = 10  # seconds
LATEST = int(time.time() - 60 * 1)
START_DATE = time.strftime("%Y-%m-%d", time.gmtime(LATEST))
TASKS: dict[int, asyncio.Task] = {}
LOGGER = logging.getLogger(__name__)

URL = "https://twitter154.p.rapidapi.com/search/search"

QUERY = {
    "query": "'pump.fun' filter:links",
    "section": "latest",
    "min_retweets": "0",
    "min_likes": "0",
    "limit": "20",
    "min_replies": "0",
    "start_date": START_DATE,
    "language": "en",
}

HEADERS = {
    "X-RapidAPI-Key": getenv("RAPIDAPI_KEY", ""),
    "X-RapidAPI-Host": "twitter154.p.rapidapi.com",
}


def determine_topic_id(follower_count: int) -> int:
    if follower_count > 1_000_000:
        return 6
    elif follower_count > 100_000:
        return 5
    elif follower_count > 10_000:
        return 4
    else:
        return 3


async def fetch_data(session: aiohttp.ClientSession) -> Optional[Dict]:
    LOGGER.info("Fetching data...")
    try:
        async with session.get(URL, headers=HEADERS, params=QUERY) as response:
            data = await response.json()
            return data
    except Exception as e:
        LOGGER.error(f"Error fetching data: {e}")
        return None


async def fetch_data_continuation(
    session: aiohttp.ClientSession, continuation: str
) -> Optional[Dict]:
    LOGGER.info("Fetching continuation data...")
    cont_query = QUERY.copy()
    cont_query["continuation_token"] = continuation
    try:
        async with session.get(URL, headers=HEADERS, params=cont_query) as response:
            data = await response.json()
            return data
    except Exception as e:
        LOGGER.error(f"Error fetching continuation data: {e}")
        return None


async def send_tweet(
    tweet: Dict, chat_id: int, topic_id: int, bot: Bot, db: MongoDB
) -> None:
    user_id = tweet["user"]["user_id"]
    user_name = tweet["user"]["username"]
    tweet_id = tweet["tweet_id"]

    sanitazed_text = await utils.replace_short_urls(tweet["text"])
    pump_url = utils.extract_url_and_validate_mint_address(sanitazed_text)
    post_url = f"https://twitter.com/{user_name}/status/{tweet_id}"

    keyboard_buttons = [
        [
            InlineKeyboardButton(
                text="Tweet",
                url=f"https://twitter.com/{user_name}/status/{tweet_id}",
            ),
            InlineKeyboardButton(
                text="Profile",
                url=f"https://x.com/{user_name}",
            ),
            InlineKeyboardButton(
                text="Block",
                callback_data=f"block:{user_name}:{user_id}",
            ),
        ],
    ]

    if pump_url:
        keyboard_buttons.append([InlineKeyboardButton(text="Pump", url=pump_url)])

    keyboard = InlineKeyboardMarkup(inline_keyboard=keyboard_buttons)

    payload = (
        f"<b>NEW TWEET</b>\n\n"
        f"{await utils.replace_short_urls(tweet['text'])}\n\n"
        f"Followers: {tweet['user']['follower_count']}\n"
    )

    attempts = 0
    max_attempts = 3

    while attempts < max_attempts:
        try:
            msg = await bot.send_message(
                chat_id=chat_id,
                message_thread_id=topic_id,
                text=payload,
                parse_mode="HTML",
                reply_markup=keyboard,
                link_preview_options=(LinkPreviewOptions(url=post_url)),
            )
            await db.update_drop_messages(tweet["user"]["user_id"], msg.message_id)
            return
        except TelegramAPIError as e:
            LOGGER.error(f"Failed to send message: {e}")
            attempts += 1
            await asyncio.sleep(1)


async def process_tweet(tweet: Dict, chat_id: int, bot: Bot, db: MongoDB) -> None:
    user_id = tweet["user"]["user_id"]
    user_name = tweet["user"]["username"]
    tweet_id = tweet["tweet_id"]

    if await db.check_banned(user_id):
        LOGGER.info(f"User {user_id} is banned")
        return

    drop = await db.get_drop(user_id)

    if drop:
        if tweet_id not in drop.get("postIds", []):
            await db.update_drop_posts(user_id, tweet_id)
        else:
            LOGGER.info(f"Tweet already exists: {tweet_id}")
            return
    else:
        await db.insert_drop(user_id, user_name, tweet_id)

    topic_id = determine_topic_id(tweet["user"]["follower_count"])

    LOGGER.info(f"New tweet found: {tweet_id}")
    await send_tweet(tweet, chat_id, topic_id, bot, db)
    await asyncio.sleep(1)


async def scheduled_function(
    session: aiohttp.ClientSession, chat_id: int, bot: Bot, db: MongoDB
) -> None:
    global LATEST
    global INTERVAL

    while True:
        try:
            data = await fetch_data(session)

            if not data or not data.get("results"):
                LOGGER.error("No results found.")
                continue

            new_latest = data["results"][0]["timestamp"]

            for tweet in data.get("results", []):
                if tweet["timestamp"] <= LATEST:
                    break

                await process_tweet(tweet, chat_id, bot, db)

            LATEST = new_latest

        except Exception as e:
            LOGGER.error(f"An error occurred: {e}")

        LOGGER.info(f"Latest Timestamp: {LATEST}. Sleeping...")
        await asyncio.sleep(INTERVAL)


async def run(chat_id: int, bot: Bot, db: MongoDB) -> None:
    if chat_id in TASKS:
        await bot.send_message(chat_id, "Scrapping is already running")
        return
    async with aiohttp.ClientSession() as session:
        task = asyncio.create_task(scheduled_function(session, chat_id, bot, db))
        TASKS[chat_id] = task
        try:
            await bot.send_message(chat_id, "Starting Twitter scrapper...")
            await task
        except asyncio.CancelledError:
            LOGGER.info(f"Task for chat_id {chat_id} was cancelled")


async def stop(chat_id: int, bot: Bot) -> None:
    task = TASKS.get(chat_id)
    if task:
        await bot.send_message(chat_id, "Stopping Twitter scrapper...")
        task.cancel()
        await task
        del TASKS[chat_id]
    else:
        await bot.send_message(chat_id, "Twitter scrapper is not running")
