---
name: using-factlane
description: يُستخدم عندما يحتاج وكيل إلى البحث في ذاكرة FactLane المحدودة أو قراءتها أو تخزينها أو تحديثها عبر MCP.
---

# استخدام FactLane

يخزن FactLane حقائق محدودة ومصحوبة بالمنشأ، لا نصوص المحادثات. الذاكرة **دليل داعم** وليست سلطة. وتتقدم التعليمات الحالية والحقيقة الحية المتحقق منها على الحقائق المتذكَّرة.

يتكون سطح MCP العام من خمس أدوات بالضبط عبر stdio. افحص مخطط MCP الحي أو `factlane --help-tools` لمعرفة الحقول وقيم enums الدقيقة؛ ولا تخمّن العقد من الأمثلة.

للـSkill المضمّن: `references/host-bootstrap.md`؛ وعلى الموقع: Quick Start.

## حدّد ما إذا كانت الذاكرة مطلوبة

- المهمة المكتفية بذاتها لا تحتاج إلى استرجاع. ابحث فقط عندما يمكن للسياق الدائم أن يساعد بصورة جوهرية.
- يحتاج التقاط Candidate إلى موافقة صريحة على المحتوى. إذا اقترح الوكيل الذاكرة، اعرض الحقيقة المحدودة واحصل على التفويض أولًا.
- الموافقة لا ترفع سلطة launcher.
- لا تنفذ `memory_store` أو `memory_update` ذاتيًا بعد انتهاء الدور.
- سجل `CANDIDATE` أو أي سجل آخر غير متحقق منه ليس `VALIDATED_CURRENT`.
- عند `status=BLOCKED`، تفرّع بحسب `error_code` الثابت. وتظل الاستثناءات غير المتوقعة أخطاء نقل.

## اقرأ داخل نطاق دقيق واحد

اختر نطاقًا دقيقًا واحدًا والمعرّفات المطلوبة من المخطط الحي.

يحظر `CROSS_PROJECT_WORKFLOW` مفاتيح هوية project/worktree/workflow/agent، بما فيها null الصريحة. وفي جلسة موثوقة ومربوطة، احذف معرّفات النطاق التي توفرها بيئة الاستضافة؛ ويجب أن تطابق المعرّفات الصريحة الروابط الموثوقة.

اختر `intent_class` وأوضاع retrieval/search من المخطط الحي.

استخدم `CURRENT` للحقائق المتحقق منها والمؤهلة، و`REVIEW_HISTORY` للفحص الصريح لـCandidate/التاريخ. واستخدم `memory_get` عندما تكون دقة المنشأ/revision مهمة.

للتاريخ الكامل، فضّل `KEYWORD` أو `EXACT` أو `memory_get` عندما يبلغ التاريخ الدلالي عن تغطية جزئية.

## اكتب فقط بسلطة موثوقة

يعمل launcher افتراضيًا بوضع read-only.

يمكن لوكيل `delegated-candidate` استدعاء `memory_store` فقط بعد موافقة صريحة على المحتوى. خزّن حقيقة محدودة واحدة مع `memory_type` و`source_provenance` و`freshness_policy` ومفتاح `idempotency_key` ثابت.

لا تشتق أو تدّعِ `authority_role` مطلقًا.

بالنسبة إلى `CROSS_PROJECT_WORKFLOW`، اتبع مخطط الحداثة الحي بدقة. ولا تبتكر حقول هوية أو fingerprint.

يمكن لمتحقق موثوق استخدام `memory_update` مع `expected_revision` الحالي ومفتاح idempotency فريد لتنفيذ `REVERIFY` أو `REPLACE`. وتتطلب ترقية Candidate استخدام `REVERIFY` مع `expected_record_id` الدقيق؛ ويستهدف `REPLACE` ذاكرة Current مؤهلة لا تعديل Candidate.

وقد تتيح سلطة Owner المباشرة أيضًا:

- `REVOKE` لسجل Current مؤهل؛
- `REJECT` لـCandidate.

تتطلب إجراءات دورة الحياة هذه `expected_record_id` الحالي الدقيق و`expected_revision` و`observed_group_token` المعروض و`reason_code` مسموحًا. لا تحاول تنفيذ إجراءات دورة حياة Owner من جلسة وكيل مفوض.

`REVOKED` و`REJECTED` حالتان منطقيتان وليستا محوًا ماديًا.

حدود database/restore/purge للـpanel المُدارة موجودة في Quick Start؛ ولا تجعل `SensitiveMemoryRecoveryOperator` العام قدرة متاحة على Windows.

## احترم حدود الحداثة وreadmission

بالنسبة إلى `on_change`، لا يراقب FactLane المصدر تلقائيًا. ينفذ Owner موثوق إعادة فحص يدوية صريحة بدليل ملاحظة حديث ومحدود.

readmission لذاكرة `REVOKED` مؤهلة عملية Owner أخرى منفصلة مع تحقق جديد وفحوص CAS. وليست استعادة لقاعدة البيانات، ويجب ألا تعيد إنشاء محتوى أزالته Recovery للذاكرة الحساسة.

## أبقِ الحقائق صغيرة وقابلة للإسناد

اجعل كل حقيقة محدودة، ومحددة النطاق، وقابلة للإسناد، ومفيدة عبر الجلسات. وأعد فحص مصدر الحقيقة الحالي قبل الاعتماد على ذاكرة قابلة للتغيّر.

لدليل التثبيت/الإصدار/المضيف، استخدم `references/host-bootstrap.md` المضمّن في Skill؛ وعلى الموقع استخدم Quick Start وRelease operations. لا تخمّن release tags.
