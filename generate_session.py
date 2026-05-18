"""
Run this script ONCE on your local machine to generate a Pyrogram session string.
You only need this if you're running a userbot (not needed for the bot-token version).

Usage:
    pip install pyrogram tgcrypto
    python3 generate_session.py

Copy the printed SESSION_STRING into your environment variables.
"""
from pyrogram import Client
from pyrogram.types import TermsOfService

API_ID   = int(input("Enter API_ID: ").strip())
API_HASH = input("Enter API_HASH: ").strip()

with Client("session_gen", api_id=API_ID, api_hash=API_HASH) as c:
    print("\n✅ Your SESSION_STRING:\n")
    print(c.export_session_string())
    print("\nStore this as the SESSION_STRING environment variable.")
