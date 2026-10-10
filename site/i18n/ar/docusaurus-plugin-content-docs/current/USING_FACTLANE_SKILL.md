---
name: using-factlane
description: يُستخدم عندما يحتاج وكيل إلى البحث في ذاكرة FactLane المحدودة أو قراءتها أو تخزينها أو تحديثها عبر MCP.
---

# استخدام FactLane

يخزّن FactLane حقائق محدودة ومصحوبة بمصدر موثّق، لا نصوص المحادثات. الذاكرة هي **دليل
مساند**، وليست أبدًا سلطة تنفيذ. تتقدم تعليمات المستخدم الحالية، وحقيقة المستودع/المنتج
الحالية، والمصادر الحية المتحقق منها على الحقائق المتذكَّرة.

## تحديد ما إذا كانت الذاكرة مطلوبة

- المهمة المكتفية بذاتها لا تحتاج إلى استرجاع سابق. ابحث فقط عندما يمكن للسياق الدائم أن يغيّر
  الإجابة أو الإجراء.
- لالتقاط Candidate مساران للموافقة: أن يطلب المستخدم صراحة تذكّر/تخزين حقيقة محدودة، أو أن
  يقترح الوكيل حقيقة قابلة لإعادة الاستخدام ثم يطلب تفويضًا صريحًا قبل تخزينها. تغطي الموافقة
  محتوى Candidate فقط؛ ولا ترفع أبدًا سلطة المشغّل/الكتابة.
- لا تنفّذ مطلقًا `memory_store` أو `memory_update` ذاتيًا بعد انتهاء الدور.
- سجل `CANDIDATE` أو `UNVERIFIED` **ليس** `VALIDATED_CURRENT`.
- افحص مخطط MCP الحي أو `factlane --help-tools` لمعرفة الحقول والتعدادات المدعومة؛
  ولا تخمّن أبدًا.

تستهدف هذه Skill **Public Contract Revision 2**.

للتثبيت المحايد للمضيف، وتسجيل Skill، والتحقق من loaded state، وأدلة التشغيل الأول، اقرأ
`references/host-bootstrap.md` من مجلد هذه Skill. لا تفترض مسارًا خاصًا بمضيف أو آلية تسجيل
لم تفحصها.

تعيد الإخفاقات المحكومة `status=BLOCKED` مع `error_code` ثابت و`message` آمنة و
`audit.retryable`؛ تفرّع بناءً على الرمز بدلًا من استخراج المعنى من نص الاستثناء.
وتظل الاستثناءات الداخلية غير المتوقعة أخطاء نقل.

## القراءة ضمن نطاق دقيق واحد

اختر نطاقًا واحدًا قبل البحث. يتطلب `PROJECT` قيمة `project_id` دقيقة؛ ويتطلب
`WORKFLOW` معرّف المشروع ذلك و`workflow_id`؛ أما `GLOBAL_USER` فغير مرتبط بمشروع؛
ويتطلب `TOOL_ENVIRONMENT` قيمة `agent_id`. ويُستخدم `CROSS_PROJECT_WORKFLOW`
لعقيدة سير العمل العابرة للمشروعات، وهو **يحظر** معرّفات المشروع وworktree وسير العمل
والوكيل، بما في ذلك قيم null الصريحة.

في جلسة موثوقة ومرتبطة، احذف المعرّفات المالكة للنطاق التي يوفّرها المضيف؛ ولا تعِد
بناءها. يجب أن تطابق المعرّفات المالكة للنطاق الصريحة القيم الموثوقة. وتظل المرشحات الأخرى
المسموح بتوجيهها من المتصل خاضعة لتوجيه المتصل.

اختر `intent_class`: `CURRENT_PROJECT_STATE` أو `PROJECT_DESIGN_RATIONALE` أو
`USER_PREFERENCE_OR_DURABLE_FACT` أو `WORKFLOW_RULE` أو `TOOL_ENVIRONMENT_STATE` أو
`HISTORICAL_QUESTION` أو `GENERAL_TASK_NO_MEMORY_REQUIRED`.

اختر `EXACT` أو `KEYWORD` أو `SEMANTIC` أو `HYBRID`. استخدم `CURRENT` للحقائق
الحالية المتحقق منها و`REVIEW_HISTORY` لفحص Candidates أو المراجعات الأقدم. ابحث
أولًا؛ ثم استخدم `memory_get` على معرّف الذاكرة المعاد عندما تكون دقة المصدر أو المراجعة
مهمة. لا تبتكر تجاوزات للتوجيه ولا تطلب توسيعًا للرسم البياني مخصصًا للمسؤول.

بالنسبة إلى `CROSS_PROJECT_WORKFLOW`، أزواج نية/وضع البحث المسموح بها هي
`WORKFLOW_RULE + CURRENT` و`WORKFLOW_RULE + REVIEW_HISTORY` و
`HISTORICAL_QUESTION + REVIEW_HISTORY`. يعيد `GENERAL_TASK_NO_MEMORY_REQUIRED`
القيمة `NO_MEMORY_NEEDED` بعد التحقق من الطلب. أما التركيبات الأخرى فتفشل بصورة
fail-closed؛ ولا يوجد fanout ضمني عبر النطاقات.

قد يزيل الضغط المتجهات من سجلات `HISTORICAL`. في `REVIEW_HISTORY`، تشير تغطية
`SEMANTIC`/`HYBRID` غير المكتملة إلى `HISTORY_SEMANTIC_PARTIAL`. فضّل `KEYWORD`
أو `EXACT` أو `memory_get` عندما تكون الحاجة إلى السجل المضغوط الكامل مهمة. ويؤدي
فقدان ميزانية النتائج بالتزامن إلى ضبط `budget.truncated=true` من دون إخفاء تدهور
السجل.

## الكتابة فقط بسلطة موثوقة

يعمل المشغّل افتراضيًا في وضع read-only. يمكن لوكيل `delegated-candidate` عادي استدعاء
`memory_store` لاقتراح Candidate، لكنه لا يستطيع استدعاء `memory_update` أو ترقية
نفسه بادعاء سلطة Owner. موافقة الدردشة لا ترفع امتيازات المشغّل/وقت التشغيل.

إذا قال المستخدم صراحة «تذكّر هذا» أو ما يعادله، فتعامل مع ذلك كموافقة على محتوى Candidate
محدود واحد فقط. وإذا نشأت الفكرة من الوكيل، فاعرض الحقيقة المقترحة بوضوح واحصل على تفويض
صريح قبل التخزين. في كلا المسارين يظل حد runtime/privacy الفعّال هو الذي يقرر إن كان التخزين
مسموحًا؛ وضع read-only يظل read-only.

خزّن حقيقة واحدة مع `memory_type` و`source_provenance` و`freshness_policy` و
`idempotency_key` مستقرة. استخدم **`source_provenance`**، لا `provenance`؛ ولا
تزوّد `authority_role` مشتقة.

بالنسبة إلى `CROSS_PROJECT_WORKFLOW`، تكون الصلاحية الزمنية `manual` (من دون مرجع
إعادة تحقق أو بصمة) أو `on_change` (قيمة `recheck_ref` غير فارغة و
`freshness_policy.source_fingerprint` مساوية لـ `source_provenance.source_hash`).
لا تضع تلك البصمة في `source_provenance` لهذا النطاق.

يمكن لمتحقق موثوق منفصل استخدام `memory_update`: اقرأ أولًا، ثم قدّم
`expected_revision` و`idempotency_key` فريدة و`REVERIFY` أو `REPLACE`. تتطلب
ترقية Candidate استخدام `REVERIFY` مع `expected_record_id` المطابق تمامًا. ويتطلب
`REPLACE` القيم `replacement.fact` و`.source_provenance` و`.freshness_policy` و
`.source_timestamp` و`.verified_by`؛ وإذا حُذفت `.last_verified_at` تُولَّد تلقائيًا.

اجعل كل ذاكرة صغيرة ومحددة النطاق وقابلة للإسناد إلى مصدر. وأعد فحص مصدر الحقيقة الحالي
قبل الاعتماد عليها.
