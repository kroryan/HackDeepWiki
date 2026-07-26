from datetime import date, timedelta

from scripts.check_pip_audit import validate_report


def _policy(*, expires: str | None = None) -> dict:
    return {
        "exceptions": [
            {
                "package": "diskcache",
                "version": "5.6.3",
                "advisory": "PYSEC-2026-2447",
                "expires": expires or str(date.today() + timedelta(days=30)),
            }
        ],
        "allowed_skips": [
            {
                "package": "engraphis",
                "version": "1.0.1",
                "reason_contains": "not found on PyPI",
            }
        ],
    }


def _report(*, advisory: str = "PYSEC-2026-2447") -> dict:
    return {
        "dependencies": [
            {
                "name": "diskcache",
                "version": "5.6.3",
                "vulns": [{"id": advisory, "aliases": []}],
            },
            {
                "name": "engraphis",
                "version": "1.0.1",
                "skip_reason": "Dependency not found on PyPI",
            },
        ]
    }


def test_approved_report_is_accepted():
    assert validate_report(_report(), _policy()) == []


def test_new_advisory_is_rejected():
    errors = validate_report(_report(advisory="NEW-CVE"), _policy())
    assert any("Unapproved vulnerability" in error for error in errors)


def test_expired_exception_is_rejected():
    policy = _policy(expires=str(date.today() - timedelta(days=1)))
    errors = validate_report(_report(), policy)
    assert any("Expired pip audit exception" in error for error in errors)


def test_unexpected_skip_is_rejected():
    report = _report()
    report["dependencies"].append(
        {"name": "mystery", "version": "1", "skip_reason": "not on index"}
    )
    errors = validate_report(report, _policy())
    assert any("Unapproved unaudited dependency" in error for error in errors)
