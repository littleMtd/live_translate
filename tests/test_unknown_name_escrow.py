from modules.unknown_name_escrow import resolve_unknown_name_escrow


def test_confirmed_unknown_names_are_escrowed_only_in_reviewed_contexts():
    cases = (
        ("\uadf8\ub7f0 \uac70\ub97c \uc0ac\uc625\uc324\uc774\ub791 \uc598\uae30\ud558\uba74", "\uc0ac\uc625\uc324"),
        ("푸코도 오늘 소 먹었네요", "푸코"),
        ("저 여고생 푸순이에요", "푸순"),
        ("모찌한테 가야 돼. 모찌한테", "모찌"),
        ("모찌야, 어디 가?", "모찌"),
    )

    for source, name in cases:
        escrow = resolve_unknown_name_escrow(source)
        assert escrow.active
        assert escrow.approved_hangul_terms == (name,)
        assert name not in escrow.provider_source
        assert escrow.provider_source.count("__LT_UNK_1__") == source.count(name)


def test_detection_does_not_generalize_to_arbitrary_honorific_or_bare_forms():
    for source in (
        "철수님이 왔어요",
        "영희 씨가 왔어요",
        "푸코는 오늘 왔어요",
        "푸순이 왔어요",
        "모찌를 보고 싶어",
        "\uc0ac\uc625\uc42c\uc774\ub791 \uc598\uae30\ud588\uc5b4",
        "\uc0ac\uc625\uc324\ub2d8\uacfc \uc598\uae30\ud588\uc5b4",
        "\ud478\ucf54\ub3c4\ub451\uc774 \uc654\uc5b4\uc694",
        "\ud478\uc21c\uc774\uc5d0\uc694\ub77c고 \ud588\uc5b4\uc694",
        "\ubaa8\ucc0c\ud55c\ud14c\ub098 \ubb3c\uc5b4\ubd10",
    ):
        escrow = resolve_unknown_name_escrow(source)
        assert not escrow.active
        assert escrow.provider_source == source


def test_known_span_wins_over_unknown_escrow():
    source = "푸코도 왔어"
    escrow = resolve_unknown_name_escrow(
        source,
        known_source_spans=((0, 2),),
    )

    assert not escrow.active


def test_placeholder_cardinality_and_restoration_are_exact():
    escrow = resolve_unknown_name_escrow("모찌한테 가야 돼. 모찌한테.")
    placeholder = escrow.entries[0].placeholder

    valid = f"得去找{placeholder}。要去{placeholder}那邊。"
    assert escrow.evaluate_provider_candidate(valid).passed
    restored = escrow.restore_provider_candidate(valid)
    assert restored == "得去找모찌。要去모찌那邊。"
    assert escrow.evaluate_final(restored).passed

    for invalid in (
        "得去找莫奇。",
        "得去找Mochi。",
        f"得去找{placeholder}。",
        f"得去找{placeholder}{placeholder}{placeholder}。",
        "得去找__LT_UNKNOWN_1__。",
        f"得去找{placeholder}和__LT_UNK_01__。",
        f"得去找{placeholder}，也叫Mochi。",
        f"得去找{placeholder}，也叫莫奇。",
    ):
        assert not escrow.evaluate_provider_candidate(invalid).passed


def test_final_invariant_rejects_loss_duplication_and_placeholder_leakage():
    escrow = resolve_unknown_name_escrow("푸순이에요")

    assert escrow.evaluate_final("我是푸순。").passed
    assert not escrow.evaluate_final("我是普順。").passed
    assert not escrow.evaluate_final("我是푸순，푸순。").passed
    assert not escrow.evaluate_final("我是__LT_UNK_1__。").passed
