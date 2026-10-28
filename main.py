import asyncio  # Provide asynchronous execution for Playwright.
import logging  # Record progress, warnings, and errors in the console.
import re  # Clean downloaded archive filenames with a regular expression.
import zipfile  # Validate ZIP archives and extract their contents.
import zlib  # Provide zlib.error, which zipfile raises for corrupt compressed data.
from pathlib import Path  # Work with local filesystem paths.
from typing import Iterator, Optional, Set  # Annotate generators, optional values, and sets.
from urllib.parse import unquote, urlparse  # Safely interpret URL paths and hostnames.
from playwright.async_api import (  # Control visible Chromium and handle browser errors.
    Error as PlaywrightError,  # Base class of every Playwright error, timeouts included.
    TimeoutError as PlaywrightTimeoutError,  # Error raised when a browser wait times out.
    async_playwright,  # Entry point that starts Playwright.
)  # Finish the Playwright import list.

START_URL = "https://www.dibbs.bsm.dla.mil/RFQ/RFQDates.aspx?category=recent"  # Official Recent RFQ Dates page.
ALLOWED_HOSTS = {  # Limit downloads to official DIBBS hosts.
    "dibbs2.bsm.dla.mil",  # Host that serves the archive downloads.
    "www.dibbs.bsm.dla.mil",  # Main DIBBS website host.
    "dibbs.bsm.dla.mil",  # Bare DIBBS domain.
}  # End of the allowed-host set.
ARCHIVE_PATH_MARKER = "/Downloads/RFQ/Archive/"  # Official RFQ archive download path.
ROOT_DIR = Path("dibbs_data")  # Store all downloaded and extracted data in this folder.
ZIP_DIR = ROOT_DIR / "zips"  # Store downloaded ZIP archives here.
EXTRACT_DIR = ROOT_DIR / "extracted"  # Extract each ZIP into its own folder here.
TIMEOUT_MS = 20000  # Standard browser operation timeout in milliseconds.
DOWNLOAD_TIMEOUT_MS = 180000  # Allow large archives three minutes (60 s could cut them off).
MAX_DOWNLOAD_ATTEMPTS = 3  # Try each plain HTTP download at most this many times.
BROWSER_DOWNLOAD_TIMEOUT_S = 60  # Seconds to wait for the browser to start a download.
ZIP_SIGNATURES = (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")  # Valid first bytes of a ZIP file.
ALWAYS_REFRESH = {"bqothers.zip"}  # Archives DIBBS rewrites under the same name, so they are downloaded on every run.

_logging_ready = False  # Track whether logging has already been configured.


def configure_logging() -> logging.Logger:  # Configure logging once and return the downloader's logger.
    global _logging_ready  # Declare that this function assigns the module-level flag.
    if not _logging_ready:  # Configure logging only on the first call.
        logging.basicConfig(  # Configure timestamped console logging.
            level=logging.INFO,  # Show INFO and more severe messages.
            format="%(asctime)s | %(levelname)s | %(message)s",  # Set the message layout.
            datefmt="%Y-%m-%d %H:%M:%S",  # Set the timestamp layout.
        )  # Finish the logging configuration.
        _logging_ready = True  # Remember that logging is now configured.
    return logging.getLogger("dibbs_downloader")  # Return a dedicated logger for this downloader.


logger = configure_logging()  # Create the logger used throughout this module.


class RunStats:  # Hold the counters that appear in the run summary.
    def __init__(self) -> None:  # Start with every counter at zero.
        self.downloaded = 0  # New ZIP files saved during this run.
        self.extracted = 0  # ZIP files extracted successfully during this run.
        self.skipped = 0  # ZIP files whose contents were already fully extracted.
        self.failed = 0  # Archives that could not be downloaded or extracted.


def safe_filename(url: str) -> str:  # Convert a download URL into a safe local filename.
    raw_name = unquote(Path(urlparse(url).path).name)  # Read and URL-decode the filename from the URL path.
    cleaned_name = re.sub(r"[^A-Za-z0-9._-]", "_", raw_name)  # Replace unsafe characters with underscores.
    return cleaned_name or "download.zip"  # Fall back to a default name if nothing usable remains.


def is_allowed_archive_url(url: str) -> bool:  # Check whether a URL points to an expected official ZIP archive.
    parsed = urlparse(url)  # Parse the candidate URL into its components.
    host_allowed = (parsed.hostname or "").lower() in ALLOWED_HOSTS  # Check the hostname against the allowlist.
    path_allowed = ARCHIVE_PATH_MARKER.lower() in parsed.path.lower()  # Check for the RFQ archive directory.
    zip_allowed = parsed.path.lower().endswith(".zip")  # Require a ZIP file path.
    return parsed.scheme == "https" and host_allowed and path_allowed and zip_allowed  # Accept only if every check passes.


def iter_file_members(archive: zipfile.ZipFile) -> Iterator[zipfile.ZipInfo]:  # Generate only the file entries of an archive.
    for member in archive.infolist():  # Visit every entry in the archive.
        if member.is_dir():  # Detect folder entries.
            continue  # Skip folders because they hold no data.
        yield member  # Hand one file entry to the caller, then pause until the next one is requested.


def is_safe_member_path(destination: Path, member_name: str) -> bool:  # Check that an entry stays inside the extraction folder.
    root = destination.resolve()  # Resolve the intended extraction directory.
    target = (root / member_name).resolve()  # Resolve where this entry would be written.
    return target == root or root in target.parents  # Allow only the folder itself or paths beneath it.


def is_extraction_complete(zip_path: Path, destination: Path) -> bool:  # Compare extracted files against the ZIP's own file list.
    if not destination.is_dir():  # A missing folder means nothing was extracted.
        return False  # Report an incomplete extraction.
    try:  # Handle unreadable archives or folders.
        with zipfile.ZipFile(zip_path, "r") as archive:  # Open the archive to read its file list.
            for member in iter_file_members(archive):  # Check every file the archive should contain.
                target = destination / member.filename  # Build the path where this file should exist.
                if not target.is_file() or target.stat().st_size != member.file_size:  # Detect a missing or truncated file.
                    return False  # One bad file makes the whole extraction incomplete.
    except (OSError, zipfile.BadZipFile):  # Treat unreadable data as incomplete.
        return False  # Report an incomplete extraction.
    return True  # Every expected file exists with the expected size.


def extract_archive(zip_path: Path, destination: Path) -> bool:  # Validate and safely extract one ZIP archive.
    try:  # Begin handling archive validation and extraction errors.
        with zipfile.ZipFile(zip_path, "r") as archive:  # Open the archive for inspection.
            bad_member = archive.testzip()  # Check every member for CRC errors.
            if bad_member is not None:  # Detect a damaged archive member.
                logger.error("ZIP validation failed for %s; damaged member: %s", zip_path.name, bad_member)  # Report the corrupt member.
                return False  # Skip extracting an invalid archive.
            for member in archive.infolist():  # Inspect each archive member before extraction.
                if not is_safe_member_path(destination, member.filename):  # Block paths that escape the extraction directory.
                    logger.error("Unsafe ZIP path detected in %s: %s", zip_path.name, member.filename)  # Log the unsafe entry.
                    return False  # Refuse to extract an archive with path traversal entries.
            destination.mkdir(parents=True, exist_ok=True)  # Create the extraction directory if needed.
            archive.extractall(destination)  # Extract the validated archive contents, overwriting any partial files.
        if not is_extraction_complete(zip_path, destination):  # Confirm that every file really landed on disk.
            logger.error("Extraction of %s finished but files are missing or the wrong size.", zip_path.name)  # Report the mismatch.
            return False  # Do not claim success for an incomplete extraction.
        logger.info("Extracted %s to %s", zip_path.name, destination)  # Report successful extraction.
        return True  # Indicate that extraction succeeded.
    except (OSError, zipfile.BadZipFile, zlib.error, EOFError, RuntimeError, ValueError) as exc:  # Catch filesystem, ZIP-format, and decompression errors.
        logger.exception("Could not extract %s: %s", zip_path.name, exc)  # Log the full error details.
        return False  # Indicate that extraction failed.


async def acknowledge_warning(page) -> bool:  # Find and click the warning page's OK control if present.
    logger.info("Checking whether the DIBBS warning page needs acknowledgement.")  # Explain the current browser step.
    candidates = [  # Define common OK control patterns used by warning pages.
        page.locator("#butAgree"),  # Match the DIBBS banner's own OK button by its id.
        page.get_by_role("button", name="OK", exact=True),  # Match a semantic button named exactly OK.
        page.locator('input[type="submit"][value="OK" i]'),  # Match a submit input whose value is OK.
        page.locator('input[type="button"][value="OK" i]'),  # Match a button input whose value is OK.
        page.get_by_role("link", name="OK", exact=True),  # Match a link named exactly OK.
        page.get_by_text("OK", exact=True),  # Use exact visible text as a final fallback.
    ]  # Finish the list of possible OK controls.
    for candidate in candidates:  # Check each possible control pattern in order.
        try:  # Handle controls that are missing, hidden, or not clickable.
            match_count = await candidate.count()  # Count every element matching this pattern.
            for index in range(match_count):  # Check every match, because the first one may be hidden.
                control = candidate.nth(index)  # Select the match at this position.
                if not await control.is_visible():  # Skip matches the user cannot see.
                    continue  # Move on to the next match.
                logger.info("Found a visible OK control; clicking it.")  # Report that the warning can be acknowledged.
                await control.click(timeout=5000, no_wait_after=True)  # Click OK without waiting for the heavy RFQ page to finish loading.
                try:  # Wait for the browser to leave the warning page.
                    await page.wait_for_url(lambda url: "dodwarning.aspx" not in url.lower(), wait_until="domcontentloaded", timeout=60000)  # Allow up to a minute for the slow RFQ page.
                except PlaywrightTimeoutError:  # Continue if the browser never leaves the warning page.
                    logger.info("The browser did not leave the warning page within 60 seconds after clicking OK.")  # Explain the timeout.
                await page.wait_for_timeout(1000)  # Allow client-side redirects or page updates to settle.
                logger.info("Current page after acknowledgement: %s", page.url)  # Record the resulting browser URL.
                return True  # Report that an OK control was clicked.
        except PlaywrightTimeoutError:  # Handle a click that timed out.
            if "dodwarning.aspx" not in page.url.lower():  # Check whether the click actually worked and only the page load was slow.
                logger.info("The click timed out, but the browser already left the warning page; treating it as acknowledged.")  # Explain the decision.
                return True  # The warning was acknowledged.
            logger.warning("An OK control was found but could not be clicked; trying another selector.")  # Report the failed attempt.
    logger.warning("No visible OK control was found on this page.")  # Explain why the script cannot proceed automatically.
    logger.info("Page title: %s", await page.title())  # Log the current page title for troubleshooting.
    body_text = (await page.locator("body").inner_text())[:3000]  # Read a limited amount of page text for diagnostics.
    logger.info("Page text for troubleshooting: %s", body_text.replace("\n", " | "))  # Log the text without flooding the console.
    return False  # Indicate that no OK control was clicked.


async def download_zip_bytes(context, archive_url: str) -> Optional[bytes]:  # Try a plain HTTP download and return the bytes only if they are a ZIP.
    attempt = 0  # Count download attempts.
    while attempt < MAX_DOWNLOAD_ATTEMPTS:  # Retry transient failures a limited number of times.
        attempt += 1  # Record this attempt.
        try:  # Handle network errors and timeouts for this attempt.
            response = await context.request.get(archive_url, timeout=DOWNLOAD_TIMEOUT_MS)  # Download using the browser context's cookies.
            payload = await response.body()  # Read the response body as bytes.
        except PlaywrightError as exc:  # Catch network failures and timeouts.
            logger.warning("Download attempt %d/%d failed for %s: %s", attempt, MAX_DOWNLOAD_ATTEMPTS, archive_url, exc)  # Report the failure.
            await asyncio.sleep(2 * attempt)  # Back off before trying again.
            continue  # Start the next attempt.
        if response.ok and payload.startswith(ZIP_SIGNATURES):  # Accept only successful responses that start like a ZIP.
            return payload  # Hand back the verified ZIP bytes.
        logger.warning(  # Explain exactly what came back instead of a ZIP.
            "Attempt %d/%d did not return a ZIP: HTTP %s, final URL %s, content-type %s, %d bytes, starts with %r",  # Message template.
            attempt,  # Current attempt number.
            MAX_DOWNLOAD_ATTEMPTS,  # Maximum number of attempts.
            response.status,  # HTTP status code received.
            response.url,  # Final URL after redirects.
            response.headers.get("content-type", "unknown"),  # Content type reported by the server.
            len(payload),  # Size of the response body.
            payload[:60],  # First bytes of the body, to recognize an HTML page.
        )  # Finish the diagnostic message.
        if response.status >= 500:  # A server-side error might be temporary.
            await asyncio.sleep(2 * attempt)  # Wait longer after each failed attempt.
        else:  # A warning banner, 404, or other answer will not change by retrying.
            break  # Stop retrying and let the caller try the browser instead.
    return None  # Report that plain HTTP could not fetch a valid ZIP.


async def browser_download(page, archive_url: str, target: Path) -> bool:  # Download one archive through the visible browser, accepting the warning banner if it appears.
    loop = asyncio.get_running_loop()  # Get the running event loop so a future can be created on it.
    download_future = loop.create_future()  # Resolve this future when the browser starts a download.

    def on_download(download) -> None:  # Receive the browser's download event.
        if not download_future.done():  # Ignore any download after the first one.
            download_future.set_result(download)  # Hand the download object to the waiting code.

    page.on("download", on_download)  # Start listening before navigating so no event is missed.
    try:  # Make sure the listener is always removed.
        try:  # Navigate like a person clicking the link on the RFQ page.
            await page.goto(archive_url, referer=START_URL, wait_until="domcontentloaded", timeout=60000)  # Open the archive URL with the RFQ page as referer.
            page_loaded = True  # A normal page loaded, so the browser did not start a download.
        except PlaywrightError as exc:  # Chromium reports a starting download as a navigation error.
            if "download is starting" not in str(exc).lower():  # Re-raise genuine navigation failures.
                raise  # Let the caller log the real error.
            page_loaded = False  # The download is starting, so no banner is in the way.
        if page_loaded:  # A page loaded instead of a download, probably the warning banner.
            button = page.locator("#butAgree")  # Find the banner's OK button.
            if await button.count() == 0:  # Detect a page that is not the banner.
                logger.error("The browser got a page instead of a ZIP: %s | title: %s", page.url, await page.title())  # Report what loaded.
                return False  # Give up on this archive.
            form_action = await page.locator("form").first.get_attribute("action")  # Read where the banner's form posts to.
            logger.info("Warning banner shown at %s (form action: %s); clicking OK.", page.url, form_action)  # Log the details for troubleshooting.
            await button.first.click(timeout=5000, no_wait_after=True)  # Click OK and let the redirect to the ZIP run.
        download = await asyncio.wait_for(download_future, timeout=BROWSER_DOWNLOAD_TIMEOUT_S)  # Wait for the browser to start the download.
        await download.save_as(target)  # Save the finished download to the target path.
        return True  # Report success.
    except asyncio.TimeoutError:  # No download started in time.
        logger.error("The browser never started a download for %s (page: %s | title: %s)", archive_url, page.url, await page.title())  # Report the stuck page.
        return False  # Report failure.
    finally:  # Always clean up the listener.
        page.remove_listener("download", on_download)  # Stop listening for downloads.


async def download_to_disk(context, page, archive_url: str, zip_path: Path, stats: RunStats, browser_hosts: Set[str]) -> bool:  # Download a ZIP, validate it, and save it safely.
    host = (urlparse(archive_url).hostname or "").lower()  # Identify which host serves this archive.
    temp_path = zip_path.with_name(zip_path.name + ".part")  # Write to a temporary name so a crash cannot leave a half-written ZIP.
    temp_path.unlink(missing_ok=True)  # Remove any leftover temporary file from an earlier run.
    saved = False  # Track whether some method produced a file.
    if host not in browser_hosts:  # Try the fast plain HTTP download first on hosts where it works.
        payload = await download_zip_bytes(context, archive_url)  # Fetch the archive bytes over HTTP.
        if payload is not None:  # Detect a successful HTTP download.
            temp_path.write_bytes(payload)  # Save the downloaded bytes to the temporary file.
            del payload  # Release the large byte string now that it is on disk.
            saved = True  # Remember that the file exists.
        else:  # Plain HTTP did not return a ZIP.
            logger.info("Plain HTTP download failed; using the browser for every archive on %s.", host)  # Explain the switch.
            browser_hosts.add(host)  # Use the browser for this host from now on.
    if not saved:  # Fall back to the browser, which behaves like a person clicking the link.
        saved = await browser_download(page, archive_url, temp_path)  # Download through the visible browser.
    if not saved or not zipfile.is_zipfile(temp_path):  # Reject a missing or non-ZIP result.
        logger.error("No valid ZIP could be saved for %s", archive_url)  # Report the failure.
        temp_path.unlink(missing_ok=True)  # Delete the unusable file.
        return False  # Report the failed download.
    size = temp_path.stat().st_size  # Read the final file size for the log.
    temp_path.replace(zip_path)  # Move the finished file onto its final name in one step.
    stats.downloaded += 1  # Count this successful download.
    logger.info("Saved %s (%d bytes)", zip_path, size)  # Report the saved file size.
    return True  # Report a successful download.


async def process_archive(context, page, archive_url: str, stats: RunStats, browser_hosts: Set[str]) -> None:  # Download (if needed) and extract one archive.
    filename = safe_filename(archive_url)  # Convert the URL filename into a safe local filename.
    zip_path = ZIP_DIR / filename  # Build the local destination path for the ZIP file.
    assert zip_path.parent == ZIP_DIR, f"Unsafe ZIP filename: {filename}"  # Guarantee the ZIP cannot land outside ZIP_DIR.
    destination = EXTRACT_DIR / Path(filename).stem  # Give each ZIP a separate extraction folder.
    logger.info("Processing archive URL: %s", archive_url)  # Report which archive is being processed.
    refresh = filename.lower() in ALWAYS_REFRESH  # Detect archives that DIBBS rewrites under the same name.
    if refresh:  # Explain why this archive is downloaded again.
        logger.info("%s is rewritten by DIBBS under the same name; downloading it again.", filename)  # Report the forced refresh.
    if not refresh and zip_path.exists() and is_extraction_complete(zip_path, destination):  # Skip only fixed archives that are fully extracted.
        logger.info("Every file from %s is already extracted; skipping.", filename)  # Report the skip.
        stats.skipped += 1  # Count the skipped archive.
        return  # Nothing more to do for this archive.
    cached = not refresh and zip_path.exists() and zipfile.is_zipfile(zip_path)  # Reuse a local ZIP only for archives that never change.
    if cached:  # A local copy exists.
        logger.info("A local ZIP already exists; trying it before downloading again: %s", zip_path)  # Report reuse.
    elif not await download_to_disk(context, page, archive_url, zip_path, stats, browser_hosts):  # Download when no usable copy exists.
        raise RuntimeError(f"Download failed for {archive_url}")  # Report the failed download to the caller.
    if extract_archive(zip_path, destination):  # Validate and extract the archive contents.
        stats.extracted += 1  # Count a successful extraction.
        return  # This archive is finished.
    zip_path.unlink(missing_ok=True)  # Delete the ZIP that would not extract so it is never reused.
    if cached:  # The old local copy may have been the problem, so try one fresh download.
        logger.warning("The local ZIP would not extract; downloading a fresh copy: %s", archive_url)  # Report the retry.
        if await download_to_disk(context, page, archive_url, zip_path, stats, browser_hosts) and extract_archive(zip_path, destination):  # Download again and extract.
            stats.extracted += 1  # Count a successful extraction.
            return  # This archive is finished.
        zip_path.unlink(missing_ok=True)  # Delete the fresh copy too if it also failed.
    raise RuntimeError(f"Could not extract a valid ZIP from {archive_url}")  # Report the failure to the caller.


async def main() -> None:  # Run the browser workflow, download archives, and extract them.
    ZIP_DIR.mkdir(parents=True, exist_ok=True)  # Create the archive download folder.
    EXTRACT_DIR.mkdir(parents=True, exist_ok=True)  # Create the extraction root folder.
    stats = RunStats()  # Create the counters for this run.
    browser_hosts: Set[str] = set()  # Remember hosts where plain HTTP downloads are blocked, so the browser is used.
    failed_requests = 0  # Count failed browser network requests.
    async with async_playwright() as playwright:  # Start Playwright and manage its resources.
        browser = await playwright.chromium.launch(headless=False)  # Launch Chromium visibly rather than headless.
        context = await browser.new_context(accept_downloads=True, ignore_https_errors=False)  # Enable downloads and keep certificate checks.
        page = await context.new_page()  # Open a new browser tab.
        page.set_default_timeout(TIMEOUT_MS)  # Apply the standard timeout to page operations.

        def on_request_failed(request) -> None:  # Count and log failed browser requests.
            nonlocal failed_requests  # Update the counter that belongs to main().
            failed_requests += 1  # Add one failed request.
            logger.warning("REQUEST FAILED: %s | %s", request.url, request.failure)  # Log the failure details.

        page.on("console", lambda message: logger.info("BROWSER CONSOLE [%s]: %s", message.type, message.text))  # Log browser console messages.
        page.on("pageerror", lambda error: logger.error("BROWSER PAGE ERROR: %s", error))  # Log uncaught JavaScript errors.
        page.on("requestfailed", on_request_failed)  # Count and log failed network requests.
        page.on("response", lambda response: logger.info("HTTP %s %s", response.status, response.url) if response.status >= 400 else logger.debug("HTTP %s %s", response.status, response.url))  # Log HTTP errors at INFO, successes at DEBUG.
        try:  # Keep browser cleanup reliable if navigation or downloading fails.
            logger.info("Opening official DIBBS Recent RFQ Dates page: %s", START_URL)  # Report the starting URL.
            response = await page.goto(START_URL, wait_until="domcontentloaded", timeout=60000)  # Navigate to the official RFQ page.
            if response is not None:  # Check whether the navigation returned an HTTP response.
                logger.info("Initial navigation returned HTTP %s.", response.status)  # Report the initial HTTP status.
            await page.wait_for_load_state("domcontentloaded", timeout=15000)  # Wait for the current document to be ready.
            warning_attempts = 0  # Count how many times the warning page was acknowledged.
            while "dodwarning.aspx" in page.url.lower() and warning_attempts < 3:  # Handle the warning page, retrying a few times.
                warning_attempts += 1  # Record this acknowledgement attempt.
                logger.info("DIBBS redirected to its warning page; attempting to click OK.")  # Explain why acknowledgement is needed.
                clicked = await acknowledge_warning(page)  # Attempt to acknowledge the warning page.
                if not clicked:  # Stop before scraping if the warning could not be acknowledged.
                    logger.error("Could not acknowledge the warning page automatically; leaving Chromium open for inspection.")  # Explain the reason.
                    await page.wait_for_timeout(30000)  # Keep the visible browser open for manual inspection.
                    return  # End this run without scraping the warning page.
                if "rfqdates.aspx" not in page.url.lower():  # Detect a redirect to some other page after acknowledging.
                    logger.info("Returning to the RFQ page after acknowledgement.")  # Explain the extra navigation.
                    await page.goto(START_URL, wait_until="domcontentloaded", timeout=60000)  # Reload the RFQ page now that consent is stored.
            if "dodwarning.aspx" in page.url.lower():  # Check whether the warning page remains after every attempt.
                logger.warning("The browser is still on the warning page after clicking OK.")  # Report the failed acknowledgement.
                logger.info("Leaving Chromium open for 30 seconds so the page can be inspected.")  # Explain the inspection pause.
                await page.wait_for_timeout(30000)  # Keep the visible browser open for manual inspection.
                return  # Stop rather than incorrectly reporting that no ZIP files exist.
            logger.info("RFQ page is ready: %s", page.url)  # Report the final page URL.
            try:  # Wait until the download links exist, because the RFQ page loads slowly.
                await page.wait_for_selector('a[href*="/downloads/rfq/archive/" i]', state="attached", timeout=60000)  # Wait for at least one archive link.
            except PlaywrightTimeoutError:  # Continue to diagnostics if no archive link ever appears.
                logger.warning("No archive link appeared within 60 seconds; continuing with what the page contains.")  # Explain the timeout.
            await page.wait_for_timeout(1500)  # Allow page scripts and link rendering to finish.
            links = await page.locator("a[href]").evaluate_all(  # Run JavaScript in the page to read every link.
                "(anchors) => anchors.map(a => ({href: a.href, text: (a.innerText || a.textContent || '').trim()}))"  # Return each URL with its label.
            )  # Collect all anchor URLs and their visible labels.
            archive_urls = sorted({item["href"] for item in links if is_allowed_archive_url(item["href"])})  # Keep unique official ZIP URLs only.
            logger.info("Found %d matching RFQ ZIP link(s).", len(archive_urls))  # Report how many archive links were found.
            if not archive_urls:  # Handle an empty link result with useful diagnostics.
                logger.warning("No matching archive links were found; logging links that mention ZIP or RFQ.")  # Explain the diagnostic fallback.
                for item in links:  # Inspect each page link for likely relevant clues.
                    if "zip" in item["href"].lower() or "rfq" in item["href"].lower() or "zip" in item["text"].lower():  # Select potentially relevant links.
                        logger.info("Possible link: text=%r url=%s", item["text"], item["href"])  # Log the possible archive link.
                logger.info("Current page title: %s", await page.title())  # Log the page title for troubleshooting.
                logger.info("Current page URL: %s", page.url)  # Log the current URL for troubleshooting.
            for archive_url in archive_urls:  # Download and extract each matching archive.
                try:  # Handle errors for each archive independently.
                    await process_archive(context, page, archive_url, stats, browser_hosts)  # Download, validate, and extract this archive.
                except Exception as exc:  # Keep one failed archive from stopping the remaining ones.
                    stats.failed += 1  # Count the failed archive.
                    logger.exception("Failed to process archive %s: %s", archive_url, exc)  # Log the complete exception details.
            logger.info("========== RUN SUMMARY ==========")  # Mark the start of the summary.
            logger.info("New ZIP downloads: %d", stats.downloaded)  # Report the number of new downloads.
            logger.info("Successful extractions: %d", stats.extracted)  # Report the number of successful extractions.
            logger.info("Already extracted (skipped): %d", stats.skipped)  # Report archives that needed no work.
            logger.info("Failures: %d", stats.failed)  # Report the number of failures.
            logger.info("Failed browser requests: %d", failed_requests)  # Report failed browser network requests.
            logger.info("ZIP folder: %s", ZIP_DIR.resolve())  # Report the full local ZIP directory.
            logger.info("Extracted data folder: %s", EXTRACT_DIR.resolve())  # Report the full local extraction directory.
            logger.info("Chromium will remain open for 30 seconds before closing.")  # Explain the final browser pause.
            await page.wait_for_timeout(30000)  # Keep the visible browser open so the final page can be inspected.
        except Exception as exc:  # Log unexpected errors in the overall browser workflow.
            logger.exception("The DIBBS downloader stopped unexpectedly: %s", exc)  # Print the complete exception details.
            logger.info("Chromium will remain open for 30 seconds for inspection if possible.")  # Explain the error inspection pause.
            try:  # Attempt to leave the browser open even after an error.
                await page.wait_for_timeout(30000)  # Keep the visible browser open for troubleshooting.
            except Exception:  # Ignore a secondary error if the page has already closed.
                pass  # Continue to browser cleanup.
        finally:  # Always clean up the browser context and browser process.
            try:  # Make sure a failure while closing the context cannot skip closing the browser.
                await context.close()  # Close the browser context and its pages.
            finally:  # Run this even if closing the context raised an error.
                await browser.close()  # Close Chromium.


if __name__ == "__main__":  # Run the asynchronous entry point only when this file is executed directly.
    asyncio.run(main())  # Start the downloader workflow.
