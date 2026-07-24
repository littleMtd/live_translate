from contextlib import contextmanager

from config import cfg
from modules.translation_corrections import load_translation_corrections
from modules.translator import (
    _CONDITIONAL_SOURCE_NORM_BY_PROFILE,
    _CONDITIONAL_SOURCE_NORM_SHARED,
    _HADES_PROFILE_ID,
    _SHARED_NAME_SCOPE,
    _SOURCE_AWARE_TARGET_REPLACEMENTS,
    _SOURCE_NORM_BY_PROFILE,
    _PROFILE_SOURCE_AWARE_TARGET_REPLACEMENTS,
    _apply_source_aware_corrections,
    _normalize_source_before_matching,
    _NAME_RENDERING_RULES,
)


@contextmanager
def _active_translation_profile(profile_id: str, use_profile: bool = True):
    original_profile = cfg.translation.streamer_profile
    original_use_profile = cfg.translation.use_profile
    object.__setattr__(cfg.translation, "streamer_profile", profile_id)
    object.__setattr__(cfg.translation, "use_profile", use_profile)
    try:
        yield
    finally:
        object.__setattr__(cfg.translation, "streamer_profile", original_profile)
        object.__setattr__(cfg.translation, "use_profile", original_use_profile)


def _wrong_profile(profile_id: str) -> str:
    return "" if profile_id else _HADES_PROFILE_ID


def test_translation_correction_data_snapshot_counts():
    tables = load_translation_corrections()

    assert len(tables.source_aware_target_replacements) == 31
    assert {profile: len(values) for profile, values in tables.source_norm_by_profile.items()} == {
        "stellive_hina": 6,
        "hades_chxxnnx": 27,
        "mwmeu": 48,
        "isegye_lilpa": 11,
        "url": 13,
    }
    assert len(tables.conditional_source_norm_shared) == 0
    assert {
        profile: len(groups)
        for profile, groups in tables.conditional_source_norm_by_profile.items()
    } == {
        "url": 2,
    }
    assert {
        profile: len(groups)
        for profile, groups in tables.profile_source_aware_target_replacements.items()
    } == {
        "stellive_hina": 5,
        "hades_chxxnnx": 1,
        "isegye_lilpa": 1,
        "url": 8,
    }
    assert len(tables.korean_name_suffixes) == 33
    assert len(tables.name_rendering_rules) == 30
    assert sum(len(rule.wrong_forms) for rule in tables.name_rendering_rules) == 237
    assert sum(
        len(group.replacements)
        for group in tables.source_aware_target_replacements
    ) == 73
    assert sum(
        len(group.replacements)
        for groups in tables.profile_source_aware_target_replacements.values()
        for group in groups
    ) == 52


def test_each_profile_source_norm_rule_triggers_and_is_gated():
    for profile_id, replacements in _SOURCE_NORM_BY_PROFILE.items():
        for noisy, canonical in replacements.items():
            with _active_translation_profile(profile_id):
                assert _normalize_source_before_matching(noisy) == canonical

            with _active_translation_profile(profile_id, use_profile=False):
                assert _normalize_source_before_matching(noisy) == noisy

            with _active_translation_profile(_wrong_profile(profile_id)):
                assert _normalize_source_before_matching(noisy) == noisy


def test_shared_source_norm_fixes_hospital_stt_mishears():
    assert _normalize_source_before_matching("운동 사면서 나왔는데요") == "운동 삼아서 나왔는데요"
    assert _normalize_source_before_matching("혈압 주셔야 되니까 빨리 오세요") == "혈압 재셔야 되니까 빨리 오세요"


def test_each_conditional_source_norm_rule_triggers_and_is_gated():
    for source_terms, replacements, match_all in _CONDITIONAL_SOURCE_NORM_SHARED:
        source = " ".join(source_terms)
        for noisy, canonical in replacements:
            candidate = f"{source} {noisy}"
            assert canonical in _normalize_source_before_matching(candidate)

    for profile_id, groups in _CONDITIONAL_SOURCE_NORM_BY_PROFILE.items():
        for source_terms, replacements, match_all in groups:
            context = " ".join(source_terms) if match_all else source_terms[0]
            for noisy, canonical in replacements:
                source = f"{context} {noisy}"
                with _active_translation_profile(profile_id):
                    assert canonical in _normalize_source_before_matching(source)

                with _active_translation_profile(profile_id, use_profile=False):
                    assert _normalize_source_before_matching(source) == source

                with _active_translation_profile(_wrong_profile(profile_id)):
                    assert _normalize_source_before_matching(source) == source

            if match_all and len(source_terms) > 1:
                noisy, _ = replacements[0]
                partial = f"{source_terms[0]} {noisy}"
                with _active_translation_profile(profile_id):
                    assert _normalize_source_before_matching(partial) == partial


def test_hospital_context_target_corrections_fix_runtime_misreads():
    cases = (
        (
            "반찬으로 나온 연근조림하고 밥만 먹고",
            "配菜裡的薑絲燉肉，然後配飯吃",
            "配菜裡的醬燒蓮藕，然後配飯吃",
        ),
        (
            "내가 배고파 이렇게 하니까",
            "我一做出餓了的樣子，媽媽就笑了",
            "我一說我餓了，媽媽就笑了",
        ),
        (
            "아프긴 한데 배고파 이러면서",
            "痛是痛，但裝作很餓的樣子",
            "痛是痛，但我餓了",
        ),
        (
            "퇴원 전에 빨리 낳고 싶기도 하고",
            "出院前想快點生下來",
            "出院前想快點好起來",
        ),
    )

    for source, target, expected in cases:
        assert _apply_source_aware_corrections(source, target) == expected


def test_hades_alias_uses_hades_profile_corrections():
    with _active_translation_profile("hades"):
        assert _normalize_source_before_matching("찬나미들 천재야") == "챈나미들 천재야"
        assert _apply_source_aware_corrections(
            "챈나미들 천재야",
            "我們-chan娜們才是天才",
        ) == "我們Chaenna粉才是天才"
        assert _apply_source_aware_corrections(
            "챈나미들 천재야",
            "我們Chaenna們才是天才",
        ) == "我們Chaenna粉才是天才"


def test_url_profile_preserves_member_names_from_runtime_variants():
    with _active_translation_profile("url"):
        assert _normalize_source_before_matching("솜명이 왔어") == "솜먕이 왔어"
        assert _normalize_source_before_matching("솜명은 어디야") == "솜먕은 어디야"

        assert _apply_source_aware_corrections(
            "마냥 랑코 아무도 못 잡을 것 같다",
            "馬樣、蘭子都抓不到呢？",
        ) == "마냥、랑코都抓不到呢？"
        assert _apply_source_aware_corrections(
            "마냥님 안 죽었네. 아이고, 모카.",
            "馬良先生還沒死。哎呀，摩卡。",
        ) == "마냥님還沒死。哎呀，모카。"
        assert _apply_source_aware_corrections(
            "오아. 오아. 마냥 랑코?",
            "噢啊。噢啊。馬朗跟蘭可？",
        ) == "오아。오아。마냥跟랑코？"
        assert _apply_source_aware_corrections(
            "마냥 언니가 개웃김",
            "明明姐姐真的超好笑",
        ) == "마냥姐姐真的超好笑"
        assert _apply_source_aware_corrections(
            "나 오아공이 두 번 살려주셨어. 모카 언니 뭐야?",
            "我被歐亞公救了兩次。摩卡姐姐，這是什麼？",
        ) == "我被오아공救了兩次。모카姐姐，這是什麼？"
        assert _apply_source_aware_corrections(
            "나 그냥 소파에 앉아있는 랑코였습니다.",
            "我只是坐在沙發上的朗科。",
        ) == "我只是坐在沙發上的랑코。"
        assert _normalize_source_before_matching(
            "손명이랑 랑코가 RPG 하는 걸 보고 싶어"
        ) == "솜먕이랑 랑코가 RPG 하는 걸 보고 싶어"
        assert _normalize_source_before_matching(
            "손명은이랑 잘 어울리겠다"
        ) == "솜먕이랑 잘 어울리겠다"
        assert _apply_source_aware_corrections(
            "손명이랑 랑코가 RPG 하는 걸 보고 싶어",
            "我想看孫明和랑코玩RPG。",
        ) == "我想看솜먕和랑코玩RPG。"
        assert _apply_source_aware_corrections(
            "그냥 손명이가 그 얘기를 들었어",
            "手明聽到了。",
        ) == "솜먕聽到了。"
        assert _apply_source_aware_corrections(
            "목화는 아무 말 안 하다가 소리 지를 것 같아",
            "木華可能一直不說話，然後突然大叫。",
        ) == "모카可能一直不說話，然後突然大叫。"
        assert _apply_source_aware_corrections(
            "랑코야, 언니한테 말이 너무 심하다.",
            "啦可呀，你對姊姊講話太過分了。",
        ) == "랑코呀，你對姊姊講話太過分了。"
        assert _apply_source_aware_corrections(
            "랑코 착하지 착한데 그래.",
            "啦科其實很善良，對啊。",
        ) == "랑코其實很善良，對啊。"
        assert _apply_source_aware_corrections(
            "마냥씨 왜요?",
            "馬尼亞小姐怎麼了？",
        ) == "마냥小姐怎麼了？"
        assert _apply_source_aware_corrections(
            "마냥 고양이처럼 따라다녔어.",
            "只是像貓一樣一直跟著。",
        ) == "只是像貓一樣一直跟著。"
        assert _apply_source_aware_corrections(
            "마냥아 오늘 7시에 뭐해?",
            "今天7點在幹嘛？",
        ) == "今天7點在幹嘛？"

    with _active_translation_profile("hades_chxxnnx"):
        assert _normalize_source_before_matching("솜명이 왔어") == "솜명이 왔어"
        assert _apply_source_aware_corrections(
            "마냥 랑코 아무도 못 잡을 것 같다",
            "馬樣、蘭子都抓不到呢？",
        ) == "馬樣、蘭子都抓不到呢？"
        assert _apply_source_aware_corrections("랑코야", "啦可呀") == "啦可呀"
        assert _apply_source_aware_corrections("마냥씨", "馬尼亞小姐") == "馬尼亞小姐"

    with _active_translation_profile("url", use_profile=False):
        assert _apply_source_aware_corrections("랑코야", "啦科呀") == "啦科呀"
        assert _apply_source_aware_corrections("마냥씨", "馬尼亞小姐") == "馬尼亞小姐"


def test_runtime_qa_safe_source_normalizations_are_profile_gated():
    cases = (
        ("다리키 엄청 심했었지", "다래끼 엄청 심했었지"),
        ("늘파님은 이미 초능력 하나 갖고 계시네요", "릴파님은 이미 초능력 하나 갖고 계시네요"),
        ("저펠구도 볼 수 있겠지", "적혈구도 볼 수 있겠지"),
        ("적혈부 봐서 뭐하네", "적혈구 봐서 뭐하네"),
        ("멀티퍼스로 인해 현재는 변하지 않는다", "멀티버스로 인해 현재는 변하지 않는다"),
        ("반, 반즈만 들었어", "반, 반주만 들었어"),
        ("그게 매트리스 세계", "그게 매트릭스 세계"),
        ("신종 연서살인마 아니야", "신종 연쇄살인마 아니야"),
        ("구독 피콘 초코케이크", "구독티콘 초코케이크"),
        ("요즘 참 소울라인 비슷한 게 많이 나와", "요즘 참 소울라이크 비슷한 게 많이 나와"),
        ("목소리를 건 패링인데", "목숨을 건 패링인데"),
    )

    with _active_translation_profile("isegye_lilpa"):
        for source, expected in cases:
            assert _normalize_source_before_matching(source) == expected

    with _active_translation_profile("url"):
        for source, _ in cases:
            assert _normalize_source_before_matching(source) == source


def test_runtime_qa_lilpa_and_ipari_rendering_variants():
    cases = (
        ("릴파님 말이 맞대요", "莉爾帕小姐說得對", "Lilpa說得對"),
        ("릴파님 사륜안", "Rilpa 先生開啟了四眼", "Lilpa開啟了寫輪眼"),
        ("늘파님은 이미 초능력이 있어요", "Neulpa先生已經有超能力", "Lilpa已經有超能力"),
        ("릴파와 놀아주는 것도 이파리의 의무지", "跟莉爾帕玩也是伊帕里的義務", "跟Lilpa玩也是이파리的義務"),
    )

    with _active_translation_profile("isegye_lilpa"):
        for source, target, expected in cases:
            assert _apply_source_aware_corrections(source, target) == expected

    with _active_translation_profile("url"):
        for source, target, _ in cases:
            assert _apply_source_aware_corrections(source, target) == target


def test_runtime_qa_url_group_and_game_terms():
    with _active_translation_profile("url"):
        assert _normalize_source_before_matching(
            "다른 URL 멤버들도 재밌어하는 것 같아"
        ) == "다른 UR:L 멤버들도 재밌어하는 것 같아"
        assert _normalize_source_before_matching("URL 로아 전파요?") == "UR:L 로스트아크 전파요?"
        assert _normalize_source_before_matching(
            "마비녹이 영웅전을 해봤지"
        ) == "마비노기 영웅전을 해봤지"
        assert _normalize_source_before_matching(
            "파스파토 2 해봤으면 좋겠다"
        ) == "Passpartout 2: The Lost Artist 해봤으면 좋겠다"

        assert _apply_source_aware_corrections(
            "UR:L 멤버들이 재밌어해",
            "其他 URL 成員們也很開心",
        ) == "其他 UR:L 成員們也很開心"
        assert _apply_source_aware_corrections(
            "로스트아크 전파요",
            "要推廣《羅亞》嗎？",
        ) == "要推廣《Lost Ark》嗎？"
        assert _apply_source_aware_corrections(
            "마비노기 영웅전을 해봤지",
            "玩過馬比諾基英雄戰。",
        ) == "玩過《新瑪奇英雄傳》。"
        assert _apply_source_aware_corrections(
            "Passpartout 2: The Lost Artist 해봤어",
            "玩過《Paspa 2》。",
        ) == "玩過《Passpartout 2: The Lost Artist》。"


def test_each_global_source_aware_rule_triggers_and_is_source_gated():
    with _active_translation_profile(""):
        for source_terms, replacements, match_all in _SOURCE_AWARE_TARGET_REPLACEMENTS:
            source = " ".join(source_terms) if match_all else source_terms[0]
            for wrong, right in replacements:
                assert _apply_source_aware_corrections(source, wrong) == right
                assert _apply_source_aware_corrections("__unrelated_source__", wrong) == wrong
            if match_all and len(source_terms) > 1:
                # A partial match must not trigger an all-terms rule.
                partial = source_terms[0]
                for wrong, right in replacements:
                    assert _apply_source_aware_corrections(partial, wrong) == wrong


def test_each_profile_source_aware_rule_triggers_and_is_profile_gated():
    for profile_id, groups in _PROFILE_SOURCE_AWARE_TARGET_REPLACEMENTS.items():
        for source_terms, replacements, match_all in groups:
            source = " ".join(source_terms) if match_all else source_terms[0]
            for wrong, right in replacements:
                with _active_translation_profile(profile_id):
                    assert _apply_source_aware_corrections(source, wrong) == right

                with _active_translation_profile(profile_id, use_profile=False):
                    assert _apply_source_aware_corrections(source, wrong) == wrong

                with _active_translation_profile(_wrong_profile(profile_id)):
                    assert _apply_source_aware_corrections(source, wrong) == wrong


def test_each_name_rendering_rule_triggers_and_is_gated():
    for rule in _NAME_RENDERING_RULES:
        wrong = next(form for form in rule.wrong_forms if form != rule.canonical)
        source = rule.source_aliases[0]

        with _active_translation_profile(rule.scope if rule.scope != _SHARED_NAME_SCOPE else ""):
            assert _apply_source_aware_corrections(source, wrong) == rule.canonical
            assert _apply_source_aware_corrections("__unrelated_source__", wrong) == wrong

        if rule.scope != _SHARED_NAME_SCOPE:
            with _active_translation_profile(rule.scope, use_profile=False):
                assert _apply_source_aware_corrections(source, wrong) == wrong

            with _active_translation_profile(_wrong_profile(rule.scope)):
                assert _apply_source_aware_corrections(source, wrong) == wrong
