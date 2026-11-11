from harness.config import load


def test_email_api_base_url_defaults_to_resend() -> None:
    cfg = load({"DATABASE_URL": "postgres://unused"})
    assert cfg.email_api_base_url == "https://api.resend.com"


def test_email_api_base_url_can_point_at_local_idempotent_double() -> None:
    cfg = load(
        {
            "DATABASE_URL": "postgres://unused",
            "EMAIL_API_BASE_URL": "http://mock-email:8099/",
        }
    )
    assert cfg.email_api_base_url == "http://mock-email:8099"
