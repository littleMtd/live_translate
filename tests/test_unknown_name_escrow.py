import json

import pytest

from modules.unknown_name_escrow import (
    load_unknown_name_policy,
    resolve_unknown_name_escrow,
)


def test_confirmed_unknown_names_are_escrowed_only_in_reviewed_contexts():
    cases = (
        ("\uadf8\ub7f0 \uac70\ub97c \uc0ac\uc625\uc324\uc774\ub791 \uc598\uae30\ud558\uba74", "\uc0ac\uc625\uc324"),
        ("푸코도 오늘 소 먹었네요", "푸코"),
        ("저 여고생 푸순이에요", "푸순"),
        ("모찌한테 가야 돼. 모찌한테", "모찌"),
        ("모찌야, 어디 가?", "모찌"),
        ("근데 랑콘님은 저는 사실", "랑콘"),
        ("아채리. 아채리가 말했어요", "아채리"),
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
        "랑콘서트가 열렸어요",
        "아채리움에 갔어요",
    ):
        escrow = resolve_unknown_name_escrow(source)
        assert not escrow.active
        assert escrow.provider_source == source


def test_explicit_first_person_identity_declaration_escrows_unknown_name():
    escrow = resolve_unknown_name_escrow("제 닉네임은 새봄이에요")

    assert escrow.approved_hangul_terms == ("새봄",)
    assert escrow.provider_source == "제 닉네임은 __LT_UNK_1__이에요"
    restored = escrow.restore_provider_candidate("我的暱稱是__LT_UNK_1__。")
    assert restored == "我的暱稱是새봄。"
    assert escrow.evaluate_final(restored).passed


def test_reviewed_name_in_identity_declaration_is_escrowed_once():
    escrow = resolve_unknown_name_escrow("제 닉네임은 푸순이에요")

    assert escrow.provider_source == "제 닉네임은 __LT_UNK_1__이에요"
    assert len(escrow.entries) == 1
    assert escrow.entries[0].source_spans == ((7, 9),)
    assert escrow.entries[0].forbidden_aliases == ("普順",)
    assert escrow.restore_provider_candidate("我的暱稱是__LT_UNK_1__。") == "我的暱稱是푸순。"


def test_generic_identity_context_rejects_ordinary_and_ambiguous_forms():
    for source in (
        "제 닉네임은 비밀이에요",
        "제 이름은 학생이에요",
        "고양이 이름은 슈슈입니다",
        "닉네임은 새봄이에요",
        "새봄님이 왔어요",
        "새봄이를 봤어요",
    ):
        assert not resolve_unknown_name_escrow(source).active


def test_explicit_member_reference_escrows_source_grounded_referent():
    escrow = resolve_unknown_name_escrow("하음이라는 멤버가 새로 들어왔어요")

    assert escrow.approved_hangul_terms == ("하음",)
    assert escrow.provider_source == "__LT_UNK_1__이라는 멤버가 새로 들어왔어요"
    assert escrow.restore_provider_candidate(
        "新加入了一位叫__LT_UNK_1__的成員。"
    ) == "新加入了一位叫하음的成員。"


def test_member_reference_context_keeps_high_precision_boundaries():
    for source in (
        "사람이라는 멤버가 필요해요",
        "학생이라는 멤버가 왔어요",
        "친구라는 멤버를 소개할게요",
        "고양이라는 멤버가 있어요",
        "막내라는 멤버를 소개할게요",
        "팀원이라는 멤버가 필요해요",
        "좋은 멤버가 필요해요",
        "학생이라는 멤버십을 샀어요",
        "대표님이 왔어요",
        "키가 작아요",
        "문제가 있어요",
    ):
        assert not resolve_unknown_name_escrow(source).active


def test_known_member_reference_span_stays_registry_owned():
    source = "랑코라는 멤버가 왔어요"
    escrow = resolve_unknown_name_escrow(source, known_source_spans=((0, 2),))

    assert not escrow.active


def test_multiple_explicit_unknown_names_have_stable_placeholders():
    escrow = resolve_unknown_name_escrow(
        "제 닉네임은 루미예요. 저의 활동명은 새봄입니다."
    )

    assert escrow.approved_hangul_terms == ("루미", "새봄")
    assert escrow.provider_source == (
        "제 닉네임은 __LT_UNK_1__예요. 저의 활동명은 __LT_UNK_2__입니다."
    )
    candidate = "我是__LT_UNK_1__，活動名是__LT_UNK_2__。"
    assert escrow.evaluate_provider_candidate(candidate).passed
    assert escrow.restore_provider_candidate(candidate) == "我是루미，活動名是새봄。"


def test_known_span_wins_over_unknown_escrow():
    source = "푸코도 왔어"
    escrow = resolve_unknown_name_escrow(
        source,
        known_source_spans=((0, 2),),
    )

    assert not escrow.active

    known_declaration = "제 닉네임은 랑코예요"
    assert not resolve_unknown_name_escrow(
        known_declaration,
        known_source_spans=((7, 9),),
    ).active


def test_policy_loader_rejects_duplicate_reviewed_names(tmp_path):
    data = {
        "schema_version": 1,
        "reviewed_exact_names": [
            {
                "source_name": "루미",
                "allowed_suffixes": ["야"],
            },
            {
                "source_name": "루미",
                "allowed_suffixes": ["한테"],
            },
        ],
        "explicit_identity_contexts": [],
    }
    path = tmp_path / "unknown_names.json"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(ValueError, match="duplicate reviewed unknown name"):
        load_unknown_name_policy(path)


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
