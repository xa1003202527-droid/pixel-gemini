"""
Google One automation using Selenium (Firefox Edition for Termux).
"""

import logging
import time
import re
from urllib.parse import urlparse
from typing import Optional

from selenium import webdriver
from selenium.common.exceptions import (
    NoSuchElementException,
    TimeoutException,
    WebDriverException,
)
from selenium.webdriver.firefox.options import Options
from selenium.webdriver.firefox.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

import config
from device_simulator import DeviceProfile

logger = logging.getLogger(__name__)


# ── Driver factory ────────────────────────────────────────────────────────────

def _build_driver(profile: DeviceProfile) -> webdriver.Firefox:
    """Return a headless Firefox WebDriver configured for the device profile."""
    options = Options()

    if config.HEADLESS:
        options.add_argument("-headless")

    # 伪装成真实的设备 User-Agent
    options.set_preference("general.useragent.override", profile.user_agent)
    # 隐藏自动化特征
    options.set_preference("dom.webdriver.enabled", False)
    # 设置窗口大小为 Pixel 10 Pro
    options.add_argument("--width=390")
    options.add_argument("--height=844")

    # Termux 专属 geckodriver 路径
    service = Service(executable_path='/data/data/com.termux/files/usr/bin/geckodriver')
    driver = webdriver.Firefox(service=service, options=options)

    driver.implicitly_wait(config.IMPLICIT_WAIT)
    driver.set_page_load_timeout(config.PAGE_LOAD_TIMEOUT)
    return driver


# ── Login helper ──────────────────────────────────────────────────────────────

def _wait_for(driver: webdriver.Firefox, by: str, value: str,
               timeout: int = config.WEBDRIVER_TIMEOUT) -> object:
    """Return element after waiting for it to be clickable."""
    return WebDriverWait(driver, timeout).until(
        EC.element_to_be_clickable((by, value))
    )


def _gmail_login(driver: webdriver.Firefox, email: str, password: str) -> bool:
    """Perform Gmail / Google account login."""
    try:
        driver.get(config.GMAIL_LOGIN_URL)
        time.sleep(2)

        # ── Email step ────────────────────────────────────────────────────────
        email_field = _wait_for(driver, By.CSS_SELECTOR, 'input[type="email"]')
        email_field.clear()
        email_field.send_keys(email)

        next_btn = _wait_for(driver, By.ID, "identifierNext")
        next_btn.click()
        time.sleep(2)

        # ── Password step ─────────────────────────────────────────────────────
        password_field = _wait_for(driver, By.CSS_SELECTOR, 'input[type="password"]')
        password_field.clear()
        password_field.send_keys(password)

        pw_next = _wait_for(driver, By.ID, "passwordNext")
        pw_next.click()
        time.sleep(3)

        # ── Verify login ──────────────────────────────────────────────────────
        current_url = driver.current_url
        parsed = urlparse(current_url)
        hostname = parsed.hostname or ""
        path = parsed.path or ""
        if (hostname == "myaccount.google.com" or hostname.endswith(".google.com") and "/u/" in path):
            logger.info("Login succeeded for %s", email)
            return True

        # Check for error messages
        try:
            error_el = driver.find_element(By.CSS_SELECTOR, '[jsname="B34EJ"], [aria-live="assertive"]')
            if error_el.text:
                logger.warning("Login error detected: %s", error_el.text)
                return False
        except NoSuchElementException:
            pass

        # If we're no longer on the login page, assume success
        if not (hostname == "accounts.google.com" and path.startswith("/signin")):
            logger.info("Login appeared successful for %s (URL: %s)", email, current_url)
            return True

        logger.warning("Unexpected URL after login: %s", current_url)
        return False

    except TimeoutException as exc:
        logger.error("Timeout during login: %s", exc)
        return False
    except WebDriverException as exc:
        logger.error("WebDriver error during login: %s", exc)
        return False


# ── Offer detection ───────────────────────────────────────────────────────────

def _extract_payment_link(driver: webdriver.Firefox) -> Optional[str]:
    """Scan the current page for a Gemini Pro offer / activation link."""
    keywords = config.GEMINI_OFFER_KEYWORDS

    all_links = driver.find_elements(By.TAG_NAME, "a")
    for link in all_links:
        try:
            text = (link.text + " " + link.get_attribute("aria-label")).lower()
            href = link.get_attribute("href") or ""
            if any(kw in text for kw in keywords) and href:
                logger.info("Found offer link via text match: %s", href)
                return href
        except Exception:
            continue

    url_patterns = re.compile(r"(gemini|upgrade|activate|offer|redeem|trial|checkout)", re.IGNORECASE)
    for link in all_links:
        try:
            href = link.get_attribute("href") or ""
            if url_patterns.search(href):
                logger.info("Found offer link via URL pattern: %s", href)
                return href
        except Exception:
            continue

    buttons = driver.find_elements(By.CSS_SELECTOR, "button, [role='button']")
    for btn in buttons:
        try:
            text = btn.text.lower()
            if any(kw in text for kw in keywords):
                try:
                    parent_link = btn.find_element(By.XPATH, "ancestor::a")
                    href = parent_link.get_attribute("href") or ""
                    if href:
                        logger.info("Found offer link via button parent: %s", href)
                        return href
                except NoSuchElementException:
                    pass
                logger.info("Found offer CTA button on page: %s", driver.current_url)
                return driver.current_url
        except Exception:
            continue

    return None


def _navigate_google_one(driver: webdriver.Firefox) -> Optional[str]:
    """Navigate to Google One and attempt to find the Gemini Pro offer link."""
    for url in (config.GOOGLE_ONE_URL, config.GOOGLE_ONE_OFFERS_URL):
        try:
            logger.info("Navigating to %s", url)
            driver.get(url)
            time.sleep(3)

            for selector in ('[aria-label="Accept all"]', 'button[jsname="higCR"]', '[data-action="accept"]'):
                try:
                    btn = driver.find_element(By.CSS_SELECTOR, selector)
                    btn.click()
                    time.sleep(1)
                    break
                except NoSuchElementException:
                    pass

            link = _extract_payment_link(driver)
            if link:
                return link

        except (TimeoutException, WebDriverException) as exc:
            logger.warning("Error accessing %s: %s", url, exc)

    return None


# ── Public API ────────────────────────────────────────────────────────────────

class GoogleAutomationError(Exception):
    """Raised when automation encounters an unrecoverable error."""


def check_gemini_offer(email: str, password: str, device: DeviceProfile) -> Optional[str]:
    """Main entry point."""
    driver: Optional[webdriver.Firefox] = None
    try:
        logger.info("Starting WebDriver for session %s", device.session_id)
        driver = _build_driver(device)

        logged_in = _gmail_login(driver, email, password)
        if not logged_in:
            raise GoogleAutomationError("Login failed – please check your credentials.")

        offer_link = _navigate_google_one(driver)
        return offer_link

    finally:
        if driver:
            try:
                driver.quit()
            except Exception:
                pass
