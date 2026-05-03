"""General-purpose async helpers and system utilities."""

import os
import re
import asyncio


async def async_run(cmd, input_data=None, timeout=30):
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.PIPE if input_data is not None else None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        inp = input_data.encode() if isinstance(input_data, str) else input_data
        stdout, stderr = await asyncio.wait_for(proc.communicate(input=inp), timeout=timeout)
        return proc.returncode, stdout.decode(errors='replace'), stderr.decode(errors='replace')
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()  # reap to avoid zombie
        return -1, "", "timeout"


async def take_screenshot(path="/tmp/screenshot.png"):
    rc, _, _ = await async_run(["screencapture", "-x", path])
    return rc == 0 and os.path.isfile(path)


def cleanup_temp(*paths):
    for p in paths:
        try:
            if os.path.isfile(p):
                os.remove(p)
        except Exception:
            pass


async def ocr_image(image_path: str) -> str:
    safe_path = image_path.replace('\\', '\\\\').replace('"', '\\"')
    script = f'''
    use framework "Vision"
    use scripting additions
    set imgPath to POSIX file "{safe_path}"
    set img to current application's NSImage's alloc()'s initWithContentsOfFile:(POSIX path of imgPath)
    if img is missing value then return ""
    set reqHandler to current application's VNImageRequestHandler's alloc()'s initWithData:(img's TIFFRepresentation()) options:(current application's NSDictionary's dictionary())
    set req to current application's VNRecognizeTextRequest's alloc()'s init()
    req's setRecognitionLevel:(current application's VNRequestTextRecognitionLevelAccurate)
    reqHandler's performRequests:(current application's NSArray's arrayWithObject:req) |error|:(missing value)
    set results to req's results()
    set output to ""
    repeat with obs in results
        set output to output & ((obs's topCandidates:1)'s first item's |string|() as text) & linefeed
    end repeat
    return output
    '''
    rc, stdout, _ = await async_run(["osascript", "-l", "AppleScript", "-e", script], timeout=30)
    return stdout.strip() if rc == 0 else ""


def parse_natural_schedule(expr: str) -> str:
    """Convert natural language schedule to cron expression. Returns expr unchanged if not recognized."""
    s = expr.lower().strip()
    # "every minute"
    if s in ("every minute", "minutely"):
        return "* * * * *"
    # "every N minutes/hours"
    m = re.match(r'every (\d+) min(?:utes?)?$', s)
    if m:
        return f"*/{m.group(1)} * * * *"
    m = re.match(r'every (\d+) hours?$', s)
    if m:
        return f"0 */{m.group(1)} * * *"
    # "every hour"
    if s in ("every hour", "hourly"):
        return "0 * * * *"
    # "every day" / "daily"
    if s in ("every day", "daily"):
        return "0 9 * * *"
    # "every week" / "weekly"
    if s in ("every week", "weekly"):
        return "0 9 * * 1"
    # "every weekday"
    if s in ("every weekday", "weekdays"):
        return "0 9 * * 1-5"
    # "every weekend"
    if s in ("every weekend", "weekends"):
        return "0 9 * * 0,6"
    # "daily at HH:MM" or "every day at HH:MM"
    m = re.match(r'(?:daily|every day)(?: at (\d{1,2})(?::(\d{2}))?\s*(am|pm)?)?$', s)
    if m and m.group(1):
        h = int(m.group(1)); mn = int(m.group(2) or 0)
        suffix = m.group(3) or ""
        if suffix == 'pm' and h < 12: h += 12
        elif suffix == 'am' and h == 12: h = 0
        return f"{mn} {h} * * *"
    # "at HH:MM" or "at H am/pm"
    m = re.match(r'at (\d{1,2})(?::(\d{2}))?\s*(am|pm)?$', s)
    if m:
        h = int(m.group(1)); mn = int(m.group(2) or 0)
        suffix = m.group(3) or ""
        if suffix == 'pm' and h < 12: h += 12
        elif suffix == 'am' and h == 12: h = 0
        return f"{mn} {h} * * *"
    # "every morning" / "every night" / "every evening"
    if s in ("every morning", "mornings"): return "0 8 * * *"
    if s in ("every night", "nightly", "every evening"): return "0 21 * * *"
    # "midnight" / "noon"
    if s == "midnight": return "0 0 * * *"
    if s == "noon": return "0 12 * * *"
    return expr  # unchanged — let croniter or simple-string matching handle it
