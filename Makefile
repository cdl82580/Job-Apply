.PHONY: test test-fast test-unit test-api test-cov test-ui test-ui-headed test-ui-local clean-cov

# ── Unit + API tests (no browser, no credentials needed) ─────────────────────

# Run the full unit/API test suite with coverage
test:
	pytest tests/ --ignore=tests/ui \
	  --cov=api --cov=apply --cov=scripts --cov=routers \
	  --cov-report=term-missing --cov-report=html:htmlcov

# Run without coverage (faster for TDD)
test-fast:
	pytest tests/ --ignore=tests/ui --no-cov -x

# Unit tests only (storage, session, webhooks, apply utils)
test-unit:
	pytest tests/test_session.py tests/test_storage.py tests/test_webhooks.py tests/test_apply_utils.py --no-cov

# API/integration tests only
test-api:
	pytest tests/test_auth.py tests/test_profile.py tests/test_health.py tests/test_runs.py \
	       tests/test_admin.py tests/test_security_headers.py tests/test_rate_limiting.py --no-cov

# Open coverage report in browser
test-cov: test
	open htmlcov/index.html

# Clean coverage artifacts
clean-cov:
	rm -rf htmlcov .coverage

# ── UI / Browser tests (requires UI_TEST_EMAIL + UI_TEST_PASSWORD) ────────────

# Run UI tests headless against the live app (default)
test-ui:
	pytest tests/ui/ \
	  --base-url=$(or $(UI_BASE_URL),https://job-apply-corey.fly.dev) \
	  --screenshot=only-on-failure \
	  --output=tests/ui/artifacts \
	  -v

# Run UI tests with a visible browser window (useful for debugging)
test-ui-headed:
	pytest tests/ui/ \
	  --base-url=$(or $(UI_BASE_URL),https://job-apply-corey.fly.dev) \
	  --headed \
	  --slowmo=200 \
	  -v

# Run UI tests against a local dev server (must be running separately)
test-ui-local:
	UI_BASE_URL=http://localhost:8000 pytest tests/ui/ \
	  --base-url=http://localhost:8000 \
	  --headed \
	  -v

# Run only the anonymous (no login required) UI tests
test-ui-anon:
	pytest tests/ui/test_login.py tests/ui/test_register.py \
	  --base-url=$(or $(UI_BASE_URL),https://job-apply-corey.fly.dev) \
	  --screenshot=only-on-failure \
	  -v

# Run only the admin UI tests (requires UI_ADMIN_EMAIL + UI_ADMIN_PASSWORD)
test-ui-admin:
	pytest tests/ui/test_admin.py \
	  --base-url=$(or $(UI_BASE_URL),https://job-apply-corey.fly.dev) \
	  --screenshot=only-on-failure \
	  -v
