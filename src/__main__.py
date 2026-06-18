"""Apify Actor entry point. Run with `python -m src`."""
import asyncio

from .main import main

asyncio.run(main())
