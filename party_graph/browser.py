"""Browser context management and anti-detection helpers."""
from __future__ import annotations

import random
import time

from party_graph.config import (
    HEADLESS,
    INIT_SCRIPT,
    UA_POOL,
    USER_DATA_DIR,
)
from party_graph.utils import log


def launch_context(pw):
    """Launch a persistent Chromium context with anti-detection measures."""
    USER_DATA_DIR.mkdir(parents=True, exist_ok=True)
    ctx = pw.chromium.launch_persistent_context(
        user_data_dir=str(USER_DATA_DIR),
        headless=HEADLESS,
        user_agent=random.choice(UA_POOL),
        viewport={
            "width": random.randint(1300, 1520),
            "height": random.randint(850, 1000),
        },
        locale="en-US",
        timezone_id="America/Los_Angeles",
        args=["--disable-blink-features=AutomationControlled"],
    )
    ctx.add_init_script(INIT_SCRIPT)
    return ctx


def human_dwell(page) -> None:
    """Simulate human reading: jittered scrolling with random pauses."""
    steps = random.randint(3, 6)
    for _ in range(steps):
        page.mouse.wheel(0, random.randint(400, 1200))
        time.sleep(random.uniform(0.5, 1.6))
    if random.random() < 0.4:
        page.evaluate("window.scrollTo(0,0)")
        time.sleep(random.uniform(0.4, 1.0))
