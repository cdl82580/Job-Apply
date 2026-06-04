.PHONY: test test-fast test-unit test-api test-cov clean-cov

# Run the full test suite with coverage
test:
	pytest

# Run without coverage (faster for TDD)
test-fast:
	pytest --no-cov -x

# Unit tests only (no API/integration tests)
test-unit:
	pytest tests/test_session.py tests/test_storage.py tests/test_webhooks.py tests/test_apply_utils.py --no-cov

# API/integration tests only
test-api:
	pytest tests/test_auth.py tests/test_profile.py tests/test_health.py tests/test_runs.py tests/test_admin.py tests/test_security_headers.py tests/test_rate_limiting.py --no-cov

# Open coverage report in browser
test-cov: test
	open htmlcov/index.html

# Clean coverage artifacts
clean-cov:
	rm -rf htmlcov .coverage
