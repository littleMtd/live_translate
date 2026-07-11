from donation_ocr.app import RecencyCache, norm_text


def test_donation_dedupe_preserves_and_compares_amounts():
    first = norm_text("donation 100 points")
    second = norm_text("donation 500 points")
    assert first != second

    cache = RecencyCache(ttl_s=180.0, similarity=0.85)
    cache.add(first, now=0.0)
    assert not cache.seen(second, now=1.0)


def test_donation_dedupe_still_suppresses_same_amount_with_punctuation_jitter():
    first = norm_text("donation! 100 points")
    second = norm_text("donation 100 points")
    cache = RecencyCache(ttl_s=180.0, similarity=0.85)
    cache.add(first, now=0.0)
    assert cache.seen(second, now=1.0)
