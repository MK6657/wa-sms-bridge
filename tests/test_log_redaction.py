from app.log_redaction import mask_identifier, mask_phone, redact_text


def test_mask_helpers_keep_edges_only():
    assert mask_phone("+79001234567") == "+7900****4567"
    assert mask_identifier("act_123456789") == "act_****6789"


def test_redact_text_masks_common_sensitive_fields():
    text = (
        "api_key=abcdef token=secret password=pw "
        "phone=+79001234567 activation=act_123456789 "
        "wa_account_id=wa_acc_111 verification_request_id=vreq_999"
    )

    redacted = redact_text(text)

    assert "abcdef" not in redacted
    assert "secret" not in redacted
    assert "+79001234567" not in redacted
    assert "act_123456789" not in redacted
    assert "wa_acc_111" not in redacted
    assert "vreq_999" not in redacted


def test_redact_text_masks_access_number_bearer_and_json_fields():
    text = (
        "Authorization: Bearer super-secret "
        "ACCESS_NUMBER:act_123456789:+79001234567 "
        '{"phone_number":"+27630445764","wa_account_id":"wa_acc_999999"}'
    )

    redacted = redact_text(text)

    assert "super-secret" not in redacted
    assert "act_123456789" not in redacted
    assert "+79001234567" not in redacted
    assert "+27630445764" not in redacted
    assert "wa_acc_999999" not in redacted
