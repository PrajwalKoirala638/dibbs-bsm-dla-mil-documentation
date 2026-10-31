# =====================================================================  # Section: imports.
# IMPORTS  # Standard library first, then Playwright.
# =====================================================================  # End of the section banner.
import asyncio  # Provide asynchronous execution for Playwright.
import logging  # Record progress, warnings, and errors in the console.
import re  # Clean URL filenames with a regular expression.
import zipfile  # Only used to confirm a downloaded file is a ZIP (nothing is extracted).
from dataclasses import dataclass  # Build the small counter class without boilerplate.
from pathlib import Path  # Work with local filesystem paths.
from typing import List, Set  # Annotate lists and sets.
from urllib.parse import unquote, urlparse  # Safely interpret URL paths and hostnames.
from playwright.async_api import (  # Control visible Chromium and handle browser errors.
    Error as PlaywrightError,  # Base class of every Playwright error, timeouts included.
    TimeoutError as PlaywrightTimeoutError,  # Error raised when a browser wait times out.
    async_playwright,  # Entry point that starts Playwright.
)  # Finish the Playwright import list.

# =====================================================================  # Section: configuration.
# CONFIGURATION  # Every tunable value lives here.
# =====================================================================  # End of the section banner.
START_URL = "https://www.dibbs.bsm.dla.mil/RFQ/RFQDates.aspx?category=recent"  # Official Recent RFQ Dates page.
ALLOWED_HOSTS = {  # Limit downloads to official DIBBS hosts.
    "dibbs2.bsm.dla.mil",  # Host that serves the archive downloads.
    "www.dibbs.bsm.dla.mil",  # Main DIBBS website host.
    "dibbs.bsm.dla.mil",  # Bare DIBBS domain.
}  # End of the allowed-host set.
ARCHIVE_PATH_MARKER = "/Downloads/RFQ/Archive/"  # Official RFQ archive download path.
ROOT_DIR = Path("dibbs_data")  # Store all downloaded data in this folder.
ZIP_DIR = ROOT_DIR / "zips"  # Store downloaded ZIP archives here (they stay zipped).
TIMEOUT_MS = 20000  # Standard browser operation timeout in milliseconds.
DOWNLOAD_TIMEOUT_MS = 180000  # Allow large archives three minutes.
MAX_DOWNLOAD_ATTEMPTS = 3  # Try each plain HTTP download at most this many times.
BROWSER_DOWNLOAD_TIMEOUT_S = 60  # Seconds to wait for the browser to start a download.
ZIP_SIGNATURES = (
    b"PK\x03\x04",
    b"PK\x05\x06",
    b"PK\x07\x08",
)  # Valid first bytes of a ZIP file.

# =====================================================================  # Section: logging.
# LOGGING  # One shared logger for the whole script.
# =====================================================================  # End of the section banner.
logging.basicConfig(  # Configure timestamped console logging.
    level=logging.INFO,  # Show INFO and more severe messages.
    format="%(asctime)s | %(levelname)s | %(message)s",  # Set the message layout.
    datefmt="%Y-%m-%d %H:%M:%S",  # Set the timestamp layout.
)  # Finish the logging configuration.
logger = logging.getLogger(
    "dibbs_downloader"
)  # Create the logger used throughout this module.


# =====================================================================  # Section: data holders.
# DATA HOLDERS  # Small classes that carry state between functions.
# =====================================================================  # End of the section banner.
@dataclass  # Generate the constructor automatically.
class RunStats:  # Hold the counters that appear in the run summary.
    downloaded: int = 0  # New ZIP files saved during this run.
    skipped: int = 0  # ZIP files that were already on disk and valid.
    failed: int = 0  # Archives that could not be downloaded.
    failed_requests: int = 0  # Failed browser network requests.


# =====================================================================  # Section: URL helpers.
# URL HELPERS  # Pure functions with no browser or disk work.
# =====================================================================  # End of the section banner.
def filename_from_url(
    url: str,
) -> str:  # Extract the archive filename from its URL, e.g. bq261001.zip.
    raw_name = unquote(
        Path(urlparse(url).path).name
    )  # Take the last part of the URL path and URL-decode it.
    cleaned_name = re.sub(
        r"[^A-Za-z0-9._-]", "_", raw_name
    )  # Replace unsafe characters with underscores.
    return (
        cleaned_name.strip(".") or "download.zip"
    )  # Never return an empty or dot-only name.


def is_allowed_archive_url(
    url: str,
) -> bool:  # Check whether a URL points to an expected official ZIP archive.
    parsed = urlparse(url)  # Parse the candidate URL into its components.
    host_allowed = (
        parsed.hostname or ""
    ).lower() in ALLOWED_HOSTS  # Check the hostname against the allowlist.
    path_allowed = (
        ARCHIVE_PATH_MARKER.lower() in parsed.path.lower()
    )  # Check for the RFQ archive directory.
    zip_allowed = parsed.path.lower().endswith(".zip")  # Require a ZIP file path.
    return (
        parsed.scheme == "https" and host_allowed and path_allowed and zip_allowed
    )  # Accept only if every check passes.


# =====================================================================  # Section: warning banner and page handling.
# BROWSER PAGE HANDLING  # Everything that reads or clicks the DIBBS website.
# =====================================================================  # End of the section banner.
async def acknowledge_warning(
    page,
) -> bool:  # Find and click the warning page's OK control if present.
    logger.info(
        "Checking whether the DIBBS warning page needs acknowledgement."
    )  # Explain the current browser step.
    candidates = [  # Define common OK control patterns used by warning pages.
        page.locator("#butAgree"),  # Match the DIBBS banner's own OK button by its id.
        page.get_by_role(
            "button", name="OK", exact=True
        ),  # Match a semantic button named exactly OK.
        page.locator(
            'input[type="submit"][value="OK" i]'
        ),  # Match a submit input whose value is OK.
        page.locator(
            'input[type="button"][value="OK" i]'
        ),  # Match a button input whose value is OK.
        page.get_by_role(
            "link", name="OK", exact=True
        ),  # Match a link named exactly OK.
        page.get_by_text(
            "OK", exact=True
        ),  # Use exact visible text as a final fallback.
    ]  # Finish the list of possible OK controls.
    for candidate in candidates:  # Check each possible control pattern in order.
        try:  # Handle controls that are missing, hidden, or not clickable.
            match_count = (
                await candidate.count()
            )  # Count every element matching this pattern.
            for index in range(
                match_count
            ):  # Check every match, because the first one may be hidden.
                control = candidate.nth(index)  # Select the match at this position.
                if not await control.is_visible():  # Skip matches the user cannot see.
                    continue  # Move on to the next match.
                logger.info(
                    "Found a visible OK control; clicking it."
                )  # Report that the warning can be acknowledged.
                await control.click(
                    timeout=5000, no_wait_after=True
                )  # Click OK without waiting for the heavy RFQ page to finish loading.
                try:  # Wait for the browser to leave the warning page.
                    await page.wait_for_url(
                        lambda url: "dodwarning.aspx" not in url.lower(),
                        wait_until="domcontentloaded",
                        timeout=60000,
                    )  # Allow up to a minute for the slow RFQ page.
                except (
                    PlaywrightTimeoutError
                ):  # Continue if the browser never leaves the warning page.
                    logger.info(
                        "The browser did not leave the warning page within 60 seconds after clicking OK."
                    )  # Explain the timeout.
                await page.wait_for_timeout(
                    1000
                )  # Allow client-side redirects or page updates to settle.
                logger.info(
                    "Current page after acknowledgement: %s", page.url
                )  # Record the resulting browser URL.
                return True  # Report that an OK control was clicked.
        except PlaywrightTimeoutError:  # Handle a click that timed out.
            if (
                "dodwarning.aspx" not in page.url.lower()
            ):  # Check whether the click actually worked and only the page load was slow.
                logger.info(
                    "The click timed out, but the browser already left the warning page; treating it as acknowledged."
                )  # Explain the decision.
                return True  # The warning was acknowledged.
            logger.warning(
                "An OK control was found but could not be clicked; trying another selector."
            )  # Report the failed attempt.
    logger.warning(
        "No visible OK control was found on this page."
    )  # Explain why the script cannot proceed automatically.
    logger.info(
        "Page title: %s", await page.title()
    )  # Log the current page title for troubleshooting.
    body_text = (await page.locator("body").inner_text())[
        :3000
    ]  # Read a limited amount of page text for diagnostics.
    logger.info(
        "Page text for troubleshooting: %s", body_text.replace("\n", " | ")
    )  # Log the text without flooding the console.
    return False  # Indicate that no OK control was clicked.


async def open_rfq_page(
    page,
) -> (
    bool
):  # Open the RFQ page, get past the warning banner, and report whether it is ready.
    logger.info(
        "Opening official DIBBS Recent RFQ Dates page: %s", START_URL
    )  # Report the starting URL.
    response = await page.goto(
        START_URL, wait_until="domcontentloaded", timeout=60000
    )  # Navigate to the official RFQ page.
    if response is not None:  # Check whether the navigation returned an HTTP response.
        logger.info(
            "Initial navigation returned HTTP %s.", response.status
        )  # Report the initial HTTP status.
    await page.wait_for_load_state(
        "domcontentloaded", timeout=15000
    )  # Wait for the current document to be ready.
    warning_attempts = 0  # Count how many times the warning page was acknowledged.
    while (
        "dodwarning.aspx" in page.url.lower() and warning_attempts < 3
    ):  # Handle the warning page, retrying a few times.
        warning_attempts += 1  # Record this acknowledgement attempt.
        logger.info(
            "DIBBS redirected to its warning page; attempting to click OK."
        )  # Explain why acknowledgement is needed.
        if not await acknowledge_warning(
            page
        ):  # Attempt to acknowledge the warning page.
            logger.error(
                "Could not acknowledge the warning page automatically; leaving Chromium open for inspection."
            )  # Explain the reason.
            await page.wait_for_timeout(
                30000
            )  # Keep the visible browser open for manual inspection.
            return False  # Tell the caller the page is not ready.
        if (
            "rfqdates.aspx" not in page.url.lower()
        ):  # Detect a redirect to some other page after acknowledging.
            logger.info(
                "Returning to the RFQ page after acknowledgement."
            )  # Explain the extra navigation.
            await page.goto(
                START_URL, wait_until="domcontentloaded", timeout=60000
            )  # Reload the RFQ page now that consent is stored.
    if (
        "dodwarning.aspx" in page.url.lower()
    ):  # Check whether the warning page remains after every attempt.
        logger.warning(
            "The browser is still on the warning page after clicking OK."
        )  # Report the failed acknowledgement.
        logger.info(
            "Leaving Chromium open for 30 seconds so the page can be inspected."
        )  # Explain the inspection pause.
        await page.wait_for_timeout(
            30000
        )  # Keep the visible browser open for manual inspection.
        return False  # Tell the caller the page is not ready.
    logger.info("RFQ page is ready: %s", page.url)  # Report the final page URL.
    return True  # Tell the caller the page is ready.


async def collect_archive_urls(
    page,
) -> List[str]:  # Read the RFQ page and return every official ZIP link.
    try:  # Wait until the download links exist, because the RFQ page loads slowly.
        await page.wait_for_selector(
            'a[href*="/downloads/rfq/archive/" i]', state="attached", timeout=60000
        )  # Wait for at least one archive link.
    except (
        PlaywrightTimeoutError
    ):  # Continue to diagnostics if no archive link ever appears.
        logger.warning(
            "No archive link appeared within 60 seconds; continuing with what the page contains."
        )  # Explain the timeout.
    await page.wait_for_timeout(
        1500
    )  # Allow page scripts and link rendering to finish.
    links = await page.locator(
        "a[href]"
    ).evaluate_all(  # Run JavaScript in the page to read every link.
        "(anchors) => anchors.map(a => ({href: a.href, text: (a.innerText || a.textContent || '').trim()}))"  # Return each URL with its label.
    )  # Collect all anchor URLs and their visible labels.
    archive_urls = sorted(
        {item["href"] for item in links if is_allowed_archive_url(item["href"])}
    )  # Keep unique official ZIP URLs only.
    logger.info(
        "Found %d matching RFQ ZIP link(s).", len(archive_urls)
    )  # Report how many archive links were found.
    if not archive_urls:  # Handle an empty link result with useful diagnostics.
        logger.warning(
            "No matching archive links were found; logging links that mention ZIP or RFQ."
        )  # Explain the diagnostic fallback.
        for item in links:  # Inspect each page link for likely relevant clues.
            if (
                "zip" in item["href"].lower()
                or "rfq" in item["href"].lower()
                or "zip" in item["text"].lower()
            ):  # Select potentially relevant links.
                logger.info(
                    "Possible link: text=%r url=%s", item["text"], item["href"]
                )  # Log the possible archive link.
        logger.info(
            "Current page title: %s", await page.title()
        )  # Log the page title for troubleshooting.
        logger.info(
            "Current page URL: %s", page.url
        )  # Log the current URL for troubleshooting.
    return archive_urls  # Hand the links back to the caller.


def attach_page_listeners(
    page, stats: RunStats
) -> None:  # Hook up logging for browser console, errors, and network failures.
    def on_request_failed(request) -> None:  # Count and log failed browser requests.
        stats.failed_requests += 1  # Add one failed request.
        logger.warning(
            "REQUEST FAILED: %s | %s", request.url, request.failure
        )  # Log the failure details.

    page.on(
        "console",
        lambda message: logger.info(
            "BROWSER CONSOLE [%s]: %s", message.type, message.text
        ),
    )  # Log browser console messages.
    page.on(
        "pageerror", lambda error: logger.error("BROWSER PAGE ERROR: %s", error)
    )  # Log uncaught JavaScript errors.
    page.on(
        "requestfailed", on_request_failed
    )  # Count and log failed network requests.
    page.on(
        "response",
        lambda response: (
            logger.info("HTTP %s %s", response.status, response.url)
            if response.status >= 400
            else logger.debug("HTTP %s %s", response.status, response.url)
        ),
    )  # Log HTTP errors at INFO, successes at DEBUG.


# =====================================================================  # Section: downloading.
# DOWNLOADING  # Two ways to fetch an archive, then the per-archive workflow.
# =====================================================================  # End of the section banner.
async def fetch_via_http(
    context, archive_url: str, temp_path: Path
) -> bool:  # Plain HTTP download that reports whether a valid ZIP was saved.
    attempt = 0  # Count download attempts.
    while (
        attempt < MAX_DOWNLOAD_ATTEMPTS
    ):  # Retry transient failures a limited number of times.
        attempt += 1  # Record this attempt.
        try:  # Handle network errors and timeouts for this attempt.
            response = await context.request.get(
                archive_url, timeout=DOWNLOAD_TIMEOUT_MS
            )  # Download using the browser context's cookies.
            payload = await response.body()  # Read the response body as bytes.
        except PlaywrightError as exc:  # Catch network failures and timeouts.
            logger.warning(
                "Download attempt %d/%d failed for %s: %s",
                attempt,
                MAX_DOWNLOAD_ATTEMPTS,
                archive_url,
                exc,
            )  # Report the failure.
            await asyncio.sleep(2 * attempt)  # Back off before trying again.
            continue  # Start the next attempt.
        if response.ok and payload.startswith(
            ZIP_SIGNATURES
        ):  # Accept only successful responses that start like a ZIP.
            temp_path.write_bytes(
                payload
            )  # Save the verified ZIP bytes to the temporary file.
            return True  # Report success.
        logger.warning(  # Explain exactly what came back instead of a ZIP.
            "Attempt %d/%d did not return a ZIP: HTTP %s, final URL %s, content-type %s, %d bytes, starts with %r",  # Message template.
            attempt,  # Current attempt number.
            MAX_DOWNLOAD_ATTEMPTS,  # Maximum number of attempts.
            response.status,  # HTTP status code received.
            response.url,  # Final URL after redirects.
            response.headers.get(
                "content-type", "unknown"
            ),  # Content type reported by the server.
            len(payload),  # Size of the response body.
            payload[:60],  # First bytes of the body, to recognize an HTML page.
        )  # Finish the diagnostic message.
        if response.status >= 500:  # A server-side error might be temporary.
            await asyncio.sleep(2 * attempt)  # Wait longer after each failed attempt.
        else:  # A warning banner, 404, or other answer will not change by retrying.
            break  # Stop retrying and let the caller try the browser instead.
    return False  # Report that plain HTTP could not fetch a valid ZIP.


async def fetch_via_browser(
    page, archive_url: str, temp_path: Path
) -> bool:  # Browser download that reports whether a file was saved.
    loop = (
        asyncio.get_running_loop()
    )  # Get the running event loop so a future can be created on it.
    download_future = (
        loop.create_future()
    )  # Resolve this future when the browser starts a download.

    def on_download(download) -> None:  # Receive the browser's download event.
        if not download_future.done():  # Ignore any download after the first one.
            download_future.set_result(
                download
            )  # Hand the download object to the waiting code.

    page.on(
        "download", on_download
    )  # Start listening before navigating so no event is missed.
    try:  # Make sure the listener is always removed.
        try:  # Navigate like a person clicking the link on the RFQ page.
            await page.goto(
                archive_url,
                referer=START_URL,
                wait_until="domcontentloaded",
                timeout=60000,
            )  # Open the archive URL with the RFQ page as referer.
            page_loaded = (
                True  # A normal page loaded, so the browser did not start a download.
            )
        except (
            PlaywrightError
        ) as exc:  # Chromium reports a starting download as a navigation error.
            if (
                "download is starting" not in str(exc).lower()
            ):  # Re-raise genuine navigation failures.
                raise  # Let the caller log the real error.
            page_loaded = False  # The download is starting, so no banner is in the way.
        if (
            page_loaded
        ):  # A page loaded instead of a download, probably the warning banner.
            button = page.locator("#butAgree")  # Find the banner's OK button.
            if await button.count() == 0:  # Detect a page that is not the banner.
                logger.error(
                    "The browser got a page instead of a ZIP: %s | title: %s",
                    page.url,
                    await page.title(),
                )  # Report what loaded.
                return False  # Give up on this archive.
            form_action = await page.locator("form").first.get_attribute(
                "action"
            )  # Read where the banner's form posts to.
            logger.info(
                "Warning banner shown at %s (form action: %s); clicking OK.",
                page.url,
                form_action,
            )  # Log the details for troubleshooting.
            await button.first.click(
                timeout=5000, no_wait_after=True
            )  # Click OK and let the redirect to the ZIP run.
        download = await asyncio.wait_for(
            download_future, timeout=BROWSER_DOWNLOAD_TIMEOUT_S
        )  # Wait for the browser to start the download.
        await download.save_as(
            temp_path
        )  # Save the finished download to the temporary path.
        return True  # Report success.
    except asyncio.TimeoutError:  # No download started in time.
        logger.error(
            "The browser never started a download for %s (page: %s | title: %s)",
            archive_url,
            page.url,
            await page.title(),
        )  # Report the stuck page.
        return False  # Report failure.
    finally:  # Always clean up the listener.
        page.remove_listener("download", on_download)  # Stop listening for downloads.


async def download_archive(
    context, page, archive_url: str, stats: RunStats, browser_hosts: Set[str]
) -> None:  # Download one archive and save it under the filename from its URL.
    logger.info(
        "Processing archive URL: %s", archive_url
    )  # Report which archive is being processed.
    filename = filename_from_url(
        archive_url
    )  # Extract the filename from the URL, e.g. bq261001.zip.
    zip_path = ZIP_DIR / filename  # Build the final local path from that filename.
    assert (
        zip_path.parent == ZIP_DIR
    ), f"Unsafe ZIP filename: {filename}"  # Guarantee the ZIP cannot land outside ZIP_DIR.
    temp_path = zip_path.with_name(
        zip_path.name + ".part"
    )  # Write to a ".part" copy so a crash cannot leave a half-written ZIP.
    logger.info(
        "Local filename from URL: %s", filename
    )  # Report the extracted filename.
    if zip_path.is_file() and zipfile.is_zipfile(
        zip_path
    ):  # Detect an archive that is already downloaded and valid.
        logger.info("%s is already downloaded; skipping.", filename)  # Report the skip.
        stats.skipped += 1  # Count the skipped archive.
        return  # Nothing more to do for this archive.
    temp_path.unlink(
        missing_ok=True
    )  # Remove any leftover temporary file from an earlier run.
    saved = False  # Track whether some method produced a file.
    host = (
        urlparse(archive_url).hostname or ""
    ).lower()  # Identify which host serves this archive.
    if (
        host not in browser_hosts
    ):  # Try the fast plain HTTP download first on hosts where it works.
        saved = await fetch_via_http(
            context, archive_url, temp_path
        )  # Fetch the archive over HTTP.
        if not saved:  # Plain HTTP did not return a ZIP.
            logger.info(
                "Plain HTTP download failed; using the browser for every archive on %s.",
                host,
            )  # Explain the switch.
            browser_hosts.add(host)  # Use the browser for this host from now on.
    if (
        not saved
    ):  # Fall back to the browser, which behaves like a person clicking the link.
        saved = await fetch_via_browser(
            page, archive_url, temp_path
        )  # Download through the visible browser.
    if not saved or not zipfile.is_zipfile(
        temp_path
    ):  # Reject a missing or non-ZIP result.
        temp_path.unlink(missing_ok=True)  # Delete the unusable file.
        raise RuntimeError(
            f"No valid ZIP could be saved for {archive_url}"
        )  # Report the failed download to the caller.
    size = temp_path.stat().st_size  # Read the final file size for the log.
    temp_path.replace(
        zip_path
    )  # Move the finished file onto its final name in one step.
    stats.downloaded += 1  # Count this saved download.
    logger.info("Saved %s (%d bytes)", zip_path, size)  # Report the saved file.


async def run_downloads(
    context, page, archive_urls: List[str], stats: RunStats
) -> None:  # Download every archive, isolating failures.
    browser_hosts: Set[str] = (
        set()
    )  # Remember hosts where plain HTTP downloads are blocked, so the browser is used.
    for archive_url in archive_urls:  # Visit each matching archive in turn.
        try:  # Handle errors for each archive independently.
            await download_archive(
                context, page, archive_url, stats, browser_hosts
            )  # Download this archive and leave it zipped.
        except (
            Exception
        ) as exc:  # Keep one failed archive from stopping the remaining ones.
            stats.failed += 1  # Count the failed archive.
            logger.exception(
                "Failed to process archive %s: %s", archive_url, exc
            )  # Log the complete exception details.


def log_summary(stats: RunStats) -> None:  # Print the end-of-run summary.
    logger.info("========== RUN SUMMARY ==========")  # Mark the start of the summary.
    logger.info(
        "New ZIP downloads: %d", stats.downloaded
    )  # Report the number of saved downloads.
    logger.info(
        "Already downloaded (skipped): %d", stats.skipped
    )  # Report archives that needed no work.
    logger.info("Failures: %d", stats.failed)  # Report the number of failures.
    logger.info(
        "Failed browser requests: %d", stats.failed_requests
    )  # Report failed browser network requests.
    logger.info(
        "ZIP folder: %s", ZIP_DIR.resolve()
    )  # Report the full local ZIP directory.


# =====================================================================  # Section: entry point.
# ENTRY POINT  # Wire everything together.
# =====================================================================  # End of the section banner.
async def main() -> None:  # Run the browser workflow and download the archives.
    ZIP_DIR.mkdir(parents=True, exist_ok=True)  # Create the archive download folder.
    stats = RunStats()  # Create the counters for this run.
    async with async_playwright() as playwright:  # Start Playwright and manage its resources.
        browser = await playwright.chromium.launch(
            headless=False
        )  # Launch Chromium visibly rather than headless.
        context = await browser.new_context(
            accept_downloads=True, ignore_https_errors=False
        )  # Enable downloads and keep certificate checks.
        page = await context.new_page()  # Open a new browser tab.
        page.set_default_timeout(
            TIMEOUT_MS
        )  # Apply the standard timeout to page operations.
        attach_page_listeners(
            page, stats
        )  # Log console output, page errors, and failed requests.
        try:  # Keep browser cleanup reliable if navigation or downloading fails.
            if not await open_rfq_page(
                page
            ):  # Stop if the RFQ page could not be reached.
                return  # End this run without scraping the warning page.
            archive_urls = await collect_archive_urls(
                page
            )  # Read the official ZIP links from the page.
            await run_downloads(
                context, page, archive_urls, stats
            )  # Download every archive under its URL filename.
            log_summary(stats)  # Print the run summary.
            logger.info(
                "Chromium will remain open for 30 seconds before closing."
            )  # Explain the final browser pause.
            await page.wait_for_timeout(
                30000
            )  # Keep the visible browser open so the final page can be inspected.
        except (
            Exception
        ) as exc:  # Log unexpected errors in the overall browser workflow.
            logger.exception(
                "The DIBBS downloader stopped unexpectedly: %s", exc
            )  # Print the complete exception details.
            logger.info(
                "Chromium will remain open for 30 seconds for inspection if possible."
            )  # Explain the error inspection pause.
            try:  # Attempt to leave the browser open even after an error.
                await page.wait_for_timeout(
                    30000
                )  # Keep the visible browser open for troubleshooting.
            except (
                Exception
            ):  # Ignore a secondary error if the page has already closed.
                pass  # Continue to browser cleanup.
        finally:  # Always clean up the browser context and browser process.
            try:  # Make sure a failure while closing the context cannot skip closing the browser.
                await context.close()  # Close the browser context and its pages.
            finally:  # Run this even if closing the context raised an error.
                await browser.close()  # Close Chromium.


if (
    __name__ == "__main__"
):  # Run the asynchronous entry point only when this file is executed directly.
    asyncio.run(main())  # Start the downloader workflow.
