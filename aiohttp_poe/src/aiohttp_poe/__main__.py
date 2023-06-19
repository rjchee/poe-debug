from aiohttp_poe.base import run
from aiohttp_poe.samples.debugbot import DebugBot
import logging

if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG)
    run(DebugBot())
