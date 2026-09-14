from equity_scanner.production_job import notification_environment


def test_external_notifications_select_only_needed_settings(tmp_path):
    source = tmp_path / 'external.env'
    source.write_text(
        'SCHWAB_GATEWAY_API_KEY=excluded\n'
        'UNRELATED_SECRET=excluded\n'
        'EQUITY_DISCORD_WEBHOOK_URL="https://example.invalid/daily"\n'
        'SEC_USER_AGENT=Scanner ops@example.invalid\n'
        'ALPHA_VANTAGE_API_KEY=example # comment\n'
    )
    assert notification_environment(source) == {
        'EQUITY_SCANNER_DISCORD_WEBHOOK_URL': 'https://example.invalid/daily',
        'SEC_USER_AGENT': 'Scanner ops@example.invalid',
        'ALPHA_VANTAGE_API_KEY': 'example',
    }
