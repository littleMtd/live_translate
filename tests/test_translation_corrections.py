from contextlib import contextmanager

from config import cfg
from modules.translation_corrections import (
    evaluate_canonical_obligations,
    load_translation_corrections,
    resolve_canonical_obligations,
)
from modules.translator import (
    _BOUNDARY_SOURCE_NORM_BY_PROFILE,
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
    get_corrections,
    reset_corrections,
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

    assert len(tables.source_aware_target_replacements) == 32
    assert {profile: len(values) for profile, values in tables.source_norm_by_profile.items()} == {
        "stellive_hina": 6,
        "hades_chxxnnx": 27,
        "mwmeu": 48,
        "isegye_lilpa": 11,
        "url": 13,
    }
    assert tables.boundary_source_norm_shared == {}
    assert {
        profile: len(values)
        for profile, values in tables.boundary_source_norm_by_profile.items()
    } == {"hades_chxxnnx": 9, "isegye_lilpa": 5}
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
    assert len(tables.name_rendering_rules) == 37
    assert sum(len(rule.wrong_forms) for rule in tables.name_rendering_rules) == 271
    assert sum(
        len(group.replacements)
        for group in tables.source_aware_target_replacements
        ) == 74
    assert sum(
        len(group.replacements)
        for groups in tables.profile_source_aware_target_replacements.values()
        for group in groups
    ) == 52


def test_required_canonical_rules_are_explicit_and_narrow():
    tables = load_translation_corrections()
    required = {
        rule.canonical
        for rule in tables.name_rendering_rules
        if rule.publication_policy == "required"
    }
    assert required - {"CHZZK", "Memnon"} == {
        "모카",
        "랑코",
        "마냥",
        "솜먕",
        "Jururu",
        "Lilpa",
        "KIIRI",
        "Heart Crush",
    }
    assert {"CHZZK", "Memnon"} <= required
    assert all(
        rule.condition_id == "always"
        for rule in tables.name_rendering_rules
        if rule.publication_policy == "required"
    )
    activation_by_canonical = {
        rule.canonical: rule.activation_policy
        for rule in tables.name_rendering_rules
        if rule.publication_policy == "required"
    }
    assert activation_by_canonical["마냥"] == "name_context_required"
    assert all(
        policy == "exact_alias"
        for canonical, policy in activation_by_canonical.items()
        if canonical != "마냥"
    )


def test_live23_entity_rescue_rules_are_exact_source_and_profile_gated():
    cases = (
        ("치지직 가기도 하고", "치지직", "CHZZK"),
        ("치지직 가기도 하고", "chzzk", "CHZZK"),
        ("아가 멤논급이야", "成員級", "Memnon級"),
    )
    for source, target, expected in cases:
        with _active_translation_profile("hades_chxxnnx"):
            assert _apply_source_aware_corrections(source, target) == expected
        with _active_translation_profile("url"):
            assert _apply_source_aware_corrections(source, target) == target
        with _active_translation_profile("hades_chxxnnx", use_profile=False):
            assert _apply_source_aware_corrections(source, target) == target
        with _active_translation_profile("hades_chxxnnx"):
            assert _apply_source_aware_corrections("관련 없는 문장", target) == target


def test_repeated_moka_particle_rescue_does_not_change_obligation_activation():
    source = "모카랑은 모카랑은 나루토 노래 부르면서 뛰어다니고."
    with _active_translation_profile("url"):
        assert (
            _apply_source_aware_corrections(source, "모카랑是邊唱歌邊跑。")
            == "모카是邊唱歌邊跑。"
        )
        obligations = resolve_canonical_obligations(
            source,
            profile_id="url",
            profile_applied=True,
            rules=_NAME_RENDERING_RULES,
            korean_name_suffixes=load_translation_corrections().korean_name_suffixes,
        )
    assert obligations == ()


def test_name_context_required_avoids_ordinary_word_collision():
    tables = load_translation_corrections()

    def targets(source: str) -> tuple[str, ...]:
        return tuple(
            obligation.canonical_target
            for obligation in resolve_canonical_obligations(
                source,
                profile_id="url",
                profile_applied=True,
                rules=tables.name_rendering_rules,
                korean_name_suffixes=tables.korean_name_suffixes,
            )
        )

    assert targets("그러니까 마냥 그냥 렛츠고 이런 느낌으로 해요.") == ()
    assert targets("마냥 씨 뭐 방송 천 일?") == ("마냥",)
    assert targets("마냥씨 뭐 방송 천 일?") == ("마냥",)
    assert targets("마냥 님 오셨어요") == ("마냥",)
    assert targets("마냥님 오셨어요") == ("마냥",)
    assert targets("마냥 언니보다 솔로곡 잘 부르면") == ("마냥",)
    assert targets("마냥언니보다 솔로곡 잘 부르면") == ("마냥",)
    assert targets("마냥 씨가 왔어") == ("마냥",)
    assert targets("마냥아 안녕") == ("마냥",)
    assert targets("마냥이 왔어") == ("마냥",)
    assert targets("마냥이가 왔어") == ("마냥",)
    assert targets("마냥이는 왔어") == ("마냥",)
    assert targets("마냥 아무 말이나 해") == ()
    assert targets("마냥아무 말이나 해") == ()
    assert targets("마냥 씨앗을 심었어") == ()


def test_ambiguous_short_name_requires_name_context():
    with _active_translation_profile("url"):
        assert _apply_source_aware_corrections(
            "오아 진짜요?", "歐亞真的嗎？"
        ) == "歐亞真的嗎？"
        assert _apply_source_aware_corrections(
            "오아님이 오셨어요", "歐亞老師來了"
        ) == "오아老師來了"


def test_streaming_hiatus_repairs_sleep_mistranslation_from_source_evidence():
    assert _apply_source_aware_corrections(
        "릴파님은 장기 휴방 중이세요", "Lilpa正在長期休眠"
    ) == "Lilpa正在長期休播"
    # 休眠 remains valid outside the evidenced long-hiatus mistranslation,
    # including when a sentence also happens to mention a stream hiatus.
    assert _apply_source_aware_corrections(
        "컴퓨터가 절전 모드예요", "電腦正在休眠"
    ) == "電腦正在休眠"
    assert _apply_source_aware_corrections(
        "휴방 중에 컴퓨터가 절전 모드에 들어갔어요",
        "休播期間電腦進入休眠",
    ) == "休播期間電腦進入休眠"


def test_collision_aware_activation_preserves_other_required_rules():
    tables = load_translation_corrections()
    obligations = resolve_canonical_obligations(
        "마냥 언니는 돌고래, 모카 언니는 니모, 솜먕 언니는 해파리",
        profile_id="url",
        profile_applied=True,
        rules=tables.name_rendering_rules,
        korean_name_suffixes=tables.korean_name_suffixes,
    )
    assert tuple(item.canonical_target for item in obligations) == (
        "모카",
        "마냥",
        "솜먕",
    )


def test_canonical_obligation_resolution_uses_profile_boundary_and_one_occurrence():
    tables = load_translation_corrections()

    obligations = resolve_canonical_obligations(
        "솜먕이 왔어",
        profile_id="url",
        profile_applied=True,
        rules=tables.name_rendering_rules,
        korean_name_suffixes=tables.korean_name_suffixes,
    )
    assert len(obligations) == 1
    assert obligations[0].matched_alias == "솜먕이"
    assert obligations[0].source_spans == ((0, 3),)
    assert obligations[0].canonical_target == "솜먕"

    assert resolve_canonical_obligations(
        "모카가 왔어",
        profile_id="irise",
        profile_applied=True,
        rules=tables.name_rendering_rules,
        korean_name_suffixes=tables.korean_name_suffixes,
    ) == ()
    assert resolve_canonical_obligations(
        "마냥히 웃었어",
        profile_id="url",
        profile_applied=True,
        rules=tables.name_rendering_rules,
        korean_name_suffixes=tables.korean_name_suffixes,
    ) == ()
    assert resolve_canonical_obligations(
        "모카랑 모카가 왔어",
        profile_id="url",
        profile_applied=True,
        rules=tables.name_rendering_rules,
        korean_name_suffixes=tables.korean_name_suffixes,
    ) == ()


def test_canonical_obligation_evaluation_never_inserts_missing_target():
    tables = load_translation_corrections()
    obligations = resolve_canonical_obligations(
        "주르르가 왔어",
        profile_id="isegye_lilpa",
        profile_applied=True,
        rules=tables.name_rendering_rules,
        korean_name_suffixes=tables.korean_name_suffixes,
    )
    passed = evaluate_canonical_obligations("Jururu來了。", obligations)
    failed = evaluate_canonical_obligations("朱嚕嚕來了。", obligations)
    embedded = evaluate_canonical_obligations("JururuExtra來了。", obligations)

    assert passed.passed and passed.satisfied == ("Jururu",)
    assert not failed.passed
    assert failed.missing == ("Jururu",)
    assert failed.rejection_reason == "canonical_obligation_missing"
    assert not embedded.passed


def test_each_profile_source_norm_rule_triggers_and_is_gated():
    for profile_id, replacements in _SOURCE_NORM_BY_PROFILE.items():
        for noisy, canonical in replacements.items():
            with _active_translation_profile(profile_id):
                assert _normalize_source_before_matching(noisy) == canonical

            with _active_translation_profile(profile_id, use_profile=False):
                assert _normalize_source_before_matching(noisy) == noisy

            with _active_translation_profile(_wrong_profile(profile_id)):
                assert _normalize_source_before_matching(noisy) == noisy


def test_each_profile_boundary_source_norm_rule_triggers_and_is_gated():
    for profile_id, replacements in _BOUNDARY_SOURCE_NORM_BY_PROFILE.items():
        for noisy, canonical in replacements.items():
            with _active_translation_profile(profile_id):
                assert _normalize_source_before_matching(noisy) == canonical

            with _active_translation_profile(profile_id, use_profile=False):
                assert _normalize_source_before_matching(noisy) == noisy

            with _active_translation_profile(_wrong_profile(profile_id)):
                assert _normalize_source_before_matching(noisy) == noisy


def test_higedan_boundary_source_norm_handles_reviewed_suffix_shapes():
    cases = {
        "희계단 분들이": "히게단 분들이",
        "희계단분들이": "히게단분들이",
        "희계단님의 공연": "히게단님의 공연",
        "희계나 님 콘서트": "히게단 님 콘서트",
        "희계나님은 멋있다": "히게단님은 멋있다",
        "희계단의 Pretender": "히게단의 Pretender",
        "희계단, 희계나!": "히게단, 히게단!",
    }
    with _active_translation_profile("isegye_lilpa"):
        for source, expected in cases.items():
            assert _normalize_source_before_matching(source) == expected


def test_higedan_boundary_source_norm_rejects_embedded_controls_and_is_idempotent():
    controls = (
        "김희계단이 왔어요",
        "희계단풍 이야기를 해요",
        "희계나무를 봤어요",
    )
    with _active_translation_profile("isegye_lilpa"):
        for source in controls:
            assert _normalize_source_before_matching(source) == source
        assert _normalize_source_before_matching("히게단 공연") == "히게단 공연"


def test_hades_lol_boundary_source_norm_is_scoped_and_collision_safe():
    cases = {
        "롤 중독 치료하는 건 없나?": "LoL 중독 치료하는 건 없나?",
        "롤은 이제 안 해요": "LoL은 이제 안 해요",
        "롤을 접겠습니다": "LoL을 접겠습니다",
        "롤, 롤 진짜 재밌어": "LoL, LoL 진짜 재밌어",
        "롤에서는 와드를 사야 해": "LoL에서는 와드를 사야 해",
        "롤에서 정글을 해": "LoL에서 정글을 해",
        "롤로 방송할까": "LoL로 방송할까",
        "롤이나 할까": "LoL이나 할까",
        "롤인데 왜 그래": "LoL인데 왜 그래",
        "롤처럼 보이네": "LoL처럼 보이네",
        "롤게임 롤랭크": "LoL 게임 LoL 랭크",
    }
    with _active_translation_profile("hades_chxxnnx"):
        for source, expected in cases.items():
            normalized = _normalize_source_before_matching(source)
            assert normalized == expected
            assert _normalize_source_before_matching(normalized) == normalized

        for control in ("롤링페이퍼", "트롤은 싫어", "컨트롤을 바꿔"):
            assert _normalize_source_before_matching(control) == control

    with _active_translation_profile("url"):
        assert _normalize_source_before_matching("롤 중독") == "롤 중독"
    with _active_translation_profile("hades_chxxnnx", use_profile=False):
        assert _normalize_source_before_matching("롤 중독") == "롤 중독"

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
        ) == "噢啊。噢啊。마냥跟랑코？"
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
        if rule.repair_requires_name_context:
            source += "님"

        with _active_translation_profile(rule.scope if rule.scope != _SHARED_NAME_SCOPE else ""):
            assert _apply_source_aware_corrections(source, wrong) == rule.canonical
            assert _apply_source_aware_corrections("__unrelated_source__", wrong) == wrong

        if rule.scope != _SHARED_NAME_SCOPE:
            with _active_translation_profile(rule.scope, use_profile=False):
                assert _apply_source_aware_corrections(source, wrong) == wrong

            with _active_translation_profile(_wrong_profile(rule.scope)):
                assert _apply_source_aware_corrections(source, wrong) == wrong


def test_irise_canonical_rendering_is_exact_profile_scoped_and_traceable():
    cases = (
        (
            "키리씨가 왔어요",
            "基里와キリ, Kiri, Kiiri, Kkiri, KIIRI",
            "KIIRI와KIIRI, KIIRI, KIIRI, KIIRI, KIIRI",
            "KIIRI",
        ),
        (
            "티즈는 준비됐어요",
            "提茲와蒂茲, Tees, Teas, ティズ",
            "TIZ와TIZ, TIZ, TIZ, TIZ",
            "TIZ",
        ),
        (
            "하트 크러쉬를 공개했어요",
            "哈特克魯什와哈特克拉什",
            "Heart Crush와Heart Crush",
            "Heart Crush",
        ),
        (
            "아이리즈의 무대예요",
            "Iris、Iris Z、IrisZ、Irises",
            "IRISÉ、IRISÉ、IRISÉ、IRISÉ",
            "IRISÉ",
        ),
    )

    with _active_translation_profile("irise"):
        for source, target, expected, canonical in cases:
            reset_corrections()
            corrected = _apply_source_aware_corrections(source, target)
            assert corrected == expected
            assert _apply_source_aware_corrections(source, corrected) == corrected
            name_traces = [
                item for item in get_corrections()
                if item["stage"] == "name_render" and item["after"] == canonical
            ]
            assert len(name_traces) == 1
            assert name_traces[0]["rule"] == f"name:{canonical}"


def test_irise_canonical_rendering_rejects_unsafe_source_activation():
    cases = (
        ("오늘 방송 재미있어요", "基里", "基里"),
        ("도키리가 왔어요", "基里", "基里"),
        ("파티즈가 시작됐어요", "蒂茲", "蒂茲"),
        ("KIIRI is here", "基里", "基里"),
        ("TIZ is here", "蒂茲", "蒂茲"),
        ("Heart Crush is out", "哈特克魯什", "哈特克魯什"),
        ("IRISÉ is back", "IrisZ", "IrisZ"),
    )

    with _active_translation_profile("irise"):
        for source, target, expected in cases:
            assert _apply_source_aware_corrections(source, target) == expected

    with _active_translation_profile("url"):
        assert _apply_source_aware_corrections("키리가 왔어요", "基里") == "基里"
    with _active_translation_profile("irise", use_profile=False):
        assert _apply_source_aware_corrections("키리가 왔어요", "基里") == "基里"


def test_irise_canonical_rendering_preserves_embedded_target_words():
    cases = (
        ("키리가 왔어요", "Kirishima, Kiri, 도키리", "Kirishima, KIIRI, 도키리"),
        ("티즈가 왔어요", "Teaspoon, Teas", "Teaspoon, TIZ"),
        ("아이리제가 왔어요", "Irisé, Iris", "Irisé, IRISÉ"),
    )

    with _active_translation_profile("irise"):
        for source, target, expected in cases:
            assert _apply_source_aware_corrections(source, target) == expected
