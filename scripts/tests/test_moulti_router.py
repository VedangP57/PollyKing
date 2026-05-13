import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))  # adds scripts/ to path

from moulti_router import (
    strip_ansi,
    classify_line,
    colorize_line,
    format_status_header,
)

RESET = '\033[0m'
GREEN = '\033[32m'
RED = '\033[31m'
BOLD_RED = '\033[1;31m'
YELLOW = '\033[33m'


def test_strip_ansi_removes_color_codes():
    assert strip_ansi('\033[32mhello\033[0m') == 'hello'
    assert strip_ansi('no codes') == 'no codes'
    assert strip_ansi('\033[1;31mbold red\033[0m text') == 'bold red text'


# classify_line ──────────────────────────────────────────────────────────────

def test_classify_gap_plain():
    assert classify_line('[10:02:11] INFO  | GAP   | KXBTCD | Gap: 7.2c') == 'gaps'


def test_classify_gap_with_ansi():
    assert classify_line('[10:02:11] INFO  | \033[33mGAP\033[0m   | KXBTCD') == 'gaps'


def test_classify_trade():
    assert classify_line('[10:03:44] INFO  | TRADE | YES Poly $10.00 | Expected: +$0.72') == 'trades'


def test_classify_rust():
    assert classify_line('[10:01:35] DEBUG | [rust] snapshot received') == 'rust_feed'


def test_classify_error():
    assert classify_line('[10:05:00] ERROR | something failed') == 'errors'


def test_classify_warning():
    assert classify_line('[10:05:00] WARNING | WebSocket disconnected: kalshi') == 'errors'


def test_classify_status_fallback():
    assert classify_line('[10:01:33] INFO  | Bot started. Mode=DRY RUN') == 'status'
    assert classify_line('[10:01:33] INFO  | WebSocket connected: kalshi') == 'status'
    assert classify_line('[10:01:33] INFO  | 80802 market pairs loaded') == 'status'


# colorize_line ──────────────────────────────────────────────────────────────

def test_colorize_trade_profit_green():
    line = 'TRADE | YES Poly $10.00 | Expected: +$0.72'
    result = colorize_line(line, 'trades')
    assert result.startswith(GREEN)
    assert result.endswith(RESET)
    assert line in result


def test_colorize_trade_loss_red():
    line = 'TRADE | YES Poly $10.00 | Expected: -$0.30'
    result = colorize_line(line, 'trades')
    assert result.startswith(RED)
    assert result.endswith(RESET)


def test_colorize_error_bold_red():
    result = colorize_line('ERROR | connection failed', 'errors')
    assert result.startswith(BOLD_RED)
    assert result.endswith(RESET)


def test_colorize_warning_yellow():
    result = colorize_line('WARNING | WebSocket disconnected', 'errors')
    assert result.startswith(YELLOW)
    assert result.endswith(RESET)


def test_colorize_gaps_passthrough():
    line = '\033[33mGAP\033[0m   | KXBTCD | Gap: 7.2c'
    assert colorize_line(line, 'gaps') == line


def test_colorize_rust_passthrough():
    line = 'DEBUG | [rust] snapshot received'
    assert colorize_line(line, 'rust_feed') == line


def test_colorize_status_passthrough():
    line = 'Bot started. Mode=DRY RUN'
    assert colorize_line(line, 'status') == line


# format_status_header ───────────────────────────────────────────────────────

def test_format_status_header_content():
    result = format_status_header('DRY RUN', 80802, 271, 12, 3)
    assert 'DRY RUN' in result
    assert '80,802' in result
    assert '00:04:31' in result
    assert '12' in result
    assert '3' in result


def test_format_status_header_live_mode():
    result = format_status_header('LIVE', 80802, 3661, 0, 0)
    assert 'LIVE' in result
    assert '01:01:01' in result


def test_format_status_header_fallback_strings():
    result = format_status_header('DRY RUN', '—', 0, '—', '—')
    assert '—' in result
    assert '00:00:00' in result


def test_format_status_header_single_line():
    result = format_status_header('DRY RUN', 80802, 60, 5, 1)
    assert '\n' not in result
