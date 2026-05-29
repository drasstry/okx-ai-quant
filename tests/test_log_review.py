from okx_ai_quant.log_review import summarize_recent_failures


def test_summarize_recent_failures_groups_common_operational_errors(tmp_path):
    bot_log = tmp_path / "okxbot.log"
    telegram_log = tmp_path / "okxbot-telegram.log"
    bot_log.write_text(
        "\n".join(
            [
                "BTC-USDT-SWAP failed: [SSL: UNEXPECTED_EOF_WHILE_READING]",
                "Could not reconcile OKX positions: handshake operation timed out",
                "Order placement failed after retries: timeout",
            ]
        ),
        encoding="utf-8",
    )
    telegram_log.write_text(
        "Telegram polling failed: The read operation timed out\n",
        encoding="utf-8",
    )

    summary = summarize_recent_failures([bot_log, telegram_log])

    assert summary["total_failures"] == 4
    names = [item["name"] for item in summary["categories"]]
    assert "okx_symbol_failed" in names
    assert "okx_reconcile_failed" in names
    assert "order_failure" in names
    assert "telegram_polling_failed" in names
    assert summary["symbol_failures"] == [{"symbol": "BTC-USDT-SWAP", "count": 1}]
    assert summary["samples"][0].startswith("BTC-USDT-SWAP failed")
