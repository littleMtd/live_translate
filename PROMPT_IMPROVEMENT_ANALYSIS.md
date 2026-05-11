# System Prompt 改进建议报告

基于数据库中的 994 条翻译记录分析（最新：2026-05-11）

## 核心问题分析

### 1. **STT 识别错误导致的翻译异常**
```
问题例子：
KO: "짠 저런 저런 너도 고지야" 
ZH: "欸，那種那種，你也是高志啊"
分析: "고지" 可能是STT识别错误，应该是人名或俚语。需要检测和修正明显错误。
```

**改进建议**：
- 加强对 **STT 噪音** 的过滤能力
- 新增规则：检测 **疑似错误翻译的关键词**（如"高志"这类生僻词汇）
- 在 **不完整或含义模糊** 的句子前加上超时机制，而不是强行翻译

---

### 2. **人名和专有名词处理不当**
```
问题例子：
KO: "나는 치코리타의 사과 내미쉬인가"
ZH: "我個人覺得奇諾比的蘋果內米什很好吃"
分析: "치코리타"、"내미쉬" 是宝可梦名称，翻译却硬生生转化为奇怪的中文名
```

**改进建议**：
- 在 `[Preserve As-Is]` 部分 **明确列出常见宝可梦名称**
- 新增 **游戏术语和角色库**
- 加强对 **品牌名、游戏名、数字内容** 的保留能力

---

### 3. **口语化和自然度不足**
```
问题例子：
KO: "아니 파이리빵은 솔직히 지금 줘도 먹을 수 있음"
ZH: "不是，帕耶利麵包老實說現在給我吃我也吃得下"
分析: 翻译虽然准确但显得呆板，缺少直播的活力感
```

**改进建议**：
- 在 `[Style]` 部分强调 **收看Vtuber直播的台湾粉丝习惯用语**
- 加入 **更多道地俚语示范**（不仅是表情符号）
- 调整 `temperature` 从 0.0 → 0.1～0.3，允许更多自然表达

---

### 4. **复杂长句的分割和理解**
```
问题例子：
KO: "근데 맛대가리도 드럽게 없어"
ZH: "但我的味蕾也爛到不行啊"
分析: 翻译可以，但 "드럽게" 这类粗俗用语的处理可以更有个性
```

**改进建议**：
- 新增 **粗俗/非正式用语库** 并标注推荐翻译
- 例如："드럽다" → "爛透了"/"糟到不行"
- 明确：**保留原有的语调强度**（不要过度柔和）

---

### 5. **数字和时间的一致性**
```
问题例子：
KO: "21개월 아 21개월을 놓고 왔습니다"
ZH: "21 個月啊，我忘了帶 21 個月"
分析: 明显是STT错误，但翻译照搬，造成意义混乱
```

**改进建议**：
- 新增 **重复数字检测**：如果同一句出现相同数字2次且逻辑不通 → 标记为疑似错误
- 在这种情况下输出 **警告** 或 **[不确定]** 前缀
- 对于 **明显无意义的重复**，考虑删除后续重复

---

### 6. **缺失的Vtuber直播文化背景**
从日志中看到很多 **个人特定的用语和情节** 未被妥善处理：
- 打工故事、日常闲聊背景
- 个人习惯用语（如"파이리빵")
- 粉丝互动模式

**改进建议**：
- 扩展 `streamer_profile` 部分，加入更多 **日常对话示范**
- 新增 **背景故事关键词库**（打工、宠物、游戏角色等）
- 定期根据直播内容更新 **专属翻译字典**

---

## 具体 System Prompt 修改建议

### 修改 1：加强 STT 错误检测
```diff
在 [Output Rules] 中添加：

+ "[STT Error Detection]"
+ "If result contains > 30% rare Korean Hangul characters with low semantic coherence, mark as [LOW_CONFIDENCE]."
+ "Examples of suspicious patterns:"
+   - Random rare surnames appearing alone (e.g., '고지야')"
+   - Same number repeated twice in short span (e.g., '21개월...21개월')"
+   - Foreign fragments with no Korean context"
```

### 修改 2：改进 [Preserve As-Is] 的专有名词库
```diff
在 [Preserve As-Is] 中扩展：

+ "Pokemon names (Chikorita, Pidgeot, etc.) — keep English or official local names."
+ "Streamer-specific slang (VVIP, 기부자, 후원자) — preserve original Korean when context is personal branding."
+ "Food brand/product names if unclear — keep original Korean rather than guessing."
```

### 修改 3：增加粗俗/非正式用语处理
```diff
在 [Style] 后添加新章节：

+ "[Colloquial & Crude Language]"
+ "드럽다 → 爛透了/糟到不行（NOT 污穢）"
+ "어쩌고/어쩔 → omit or keep as ellipsis if used as filler"
+ "막/맨날 (verbal habit) → 每天/一直/就是 (depending on context)"
+ "Preserve speaker's casual tone; do NOT sanitize or over-formalize."
```

### 修改 4：降低 temperature 以提高一致性
在 `config.py` 中考虑修改：
```diff
- "temperature": 0.0,
+ "temperature": 0.1,  # Allow slight variation for more natural phrasing
```

### 修改 5：扩展直播文化示范
在现有 `_STREAMER_PROFILES` 示范中添加更多 **日常背景** 的例子：
```diff
+ "例 84（打工日常）"
+ "input: 편의점 알바 진짜 힘들었어"
+ "output: 便利商店打工真的超累"
+ ""
+ "例 85（游戏失败）"
+ "input: 아 또 죽었다 진짜 어렵네"
+ "output: 啊又死了，真的好難欸"
```

---

## 优先级排序

| 优先级 | 改进项 | 影响范围 | 实施难度 |
|------|--------|---------|---------|
| **P0** | 加强STT错误检测 | 所有含STT噪音的翻译 | 中 |
| **P1** | 扩展专有名词库（宝可梦、品牌等） | 游戏直播翻译 | 低 |
| **P1** | 粗俗/俚语库规范化 | 口语化表达 | 低 |
| **P2** | 调整 temperature 参数 | 整体自然度 | 极低 |
| **P2** | 更新 streamer_profile 示范 | 特定主播准确度 | 低 |
| **P3** | 重复数字检测 | 边界情况处理 | 中 |

---

## 快速修复建议（可立即实施）

1. **添加更多俚语映射** 到 `config.py` 的 slang 字典中
2. **扩展 Preserve As-Is 规则** 列表，包含常见游戏名称
3. **在示范中添加** "不完整句子 + STT错误" 的处理案例
4. **调高 temperature** 值到 0.1～0.2

