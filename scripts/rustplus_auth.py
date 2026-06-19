#!/usr/bin/env python3
"""
Rust+ Authentication Script

This script logs into Steam and retrieves the necessary FCM credentials
to use with the Home Assistant Rust+ integration.
"""
import sys
import json
import logging
import asyncio
from steam.client import SteamClient
import requests

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("rustplus_auth")

def get_fcm_credentials(steam_id: int, auth_ticket: str):
    """Retrieve FCM credentials from Facepunch API using Steam auth ticket."""
    logger.info("Requesting FCM credentials from Facepunch...")
    # NOTE: The Facepunch API endpoints for FCM token generation requires specific
    # app id and cryptographic signatures not publicly documented or easily replicated
    # outside of the official iOS/Android apps or specialized proxies.
    # The rustplus node.js ecosystem relies on a proxy extension.
    # For a fully native Python solution without the companion app, we'd need
    # Google Play Services GCM/FCM registration logic which is highly complex.

    # As a fallback for this initial version (and as noted by the user),
    # we output instructions on how to use the standard companion extension
    # to obtain the exact JSON block until a reverse-engineered Python GCM
    # registration port is available.

    print("\n--- Rust+ FCM Credentials Setup ---")
    print("Currently, native Python FCM registration for Rust+ is unsupported due to GCM/FCM certificate pinning.")
    print("Please use the Rust+ Companion Extension to get your credentials:")
    print("1. Install 'RustPlusPlus' or 'companion-rust' Chrome Extension.")
    print("2. Link your Steam Account.")
    print("3. Pair your server from the game.")
    print("4. Copy the generated configuration block.")
    print("5. Paste the FCM credentials, Server IP, Port, Player ID, and Token into Home Assistant.\n")
    return None

def main():
    print("Rust+ Home Assistant Auth Script")
    print("--------------------------------")

    # Check if they want to try steam login anyway
    choice = input("Do you want to test Steam Login? (y/n): ")
    if choice.lower() != 'y':
        get_fcm_credentials(0, "")
        sys.exit(0)

    client = SteamClient()

    username = input("Steam Username: ")
    password = input("Steam Password: ")

    logger.info("Logging into Steam...")
    result = client.cli_login(username, password)

    if result != 1:  # 1 is EResult.OK
        logger.error(f"Failed to login to Steam. Result: {result}")
        sys.exit(1)

    logger.info(f"Successfully logged in as {client.user.name} (SteamID: {client.steam_id.as_64})")

    # Get App Ticket for Rust (AppID 252490)
    logger.info("Requesting Rust app ticket...")
    ticket = client.get_app_ticket(252490)

    if not ticket:
        logger.error("Failed to get app ticket for Rust.")
        client.logout()
        sys.exit(1)

    auth_ticket = ticket.ticket.hex()
    logger.info(f"Acquired auth ticket.")

    get_fcm_credentials(client.steam_id.as_64, auth_ticket)
    client.logout()

if __name__ == "__main__":
    main()
