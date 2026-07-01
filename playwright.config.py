"""
Playwright configuration for UI tests.

Run with:
    pytest tests/ui/                         # headless (default)
    pytest tests/ui/ --headed                # show browser window
    pytest tests/ui/ --slowmo=300            # slow down for debugging
    pytest tests/ui/ --screenshot=only-on-failure
    pytest tests/ui/ -k "test_login"         # run a specific test

Set environment variables for target and credentials:
    UI_BASE_URL=https://apply.cdlav.us
    UI_TEST_EMAIL=your@email.com
    UI_TEST_PASSWORD=yourpassword

Or point at local dev:
    UI_BASE_URL=http://localhost:8000
"""
