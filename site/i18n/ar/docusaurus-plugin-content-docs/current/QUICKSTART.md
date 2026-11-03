# البدء السريع

يشرح هذا الدليل إعداد خادم FactLane MCP محلي وربطه بوكيل. الخادم **عملية stdio يبدأها MCP host**،
وليس خدمة ويب أو تطبيقًا تفاعليًا في الطرفية. وهو يخزّن حقائق محدودة، لا نصوص المحادثات.

## كيف يبدو النجاح

في الإعداد الأول، اجعل الهدف محدودًا. تكون قد انتهيت عندما:

1. يستطيع MCP host المتوافق تشغيل FactLane؛
2. تظهر أدوات FactLane الخمس بالضبط؛
3. ينجح طلب `memory_status` للقراءة فقط ضمن نطاق دقيق.

ابدأ بوضع القراءة فقط. لا تفعّل كتابة Candidate إلا بعد نجاح الاتصال الأساسي.

## 1. اختر إصدارًا منشورًا محددًا وتحقق من بيئة التشغيل

قبل تغيير الجهاز، افصل بين **الفحص** و**التعديل**. قراءة إعداد الـhost الحالي وهوية runtime المثبّتة
ومسارات التخزين ووثائق الإصدار هي أعمال للقراءة فقط. أما استنساخ الحزم أو تثبيتها، وسحب نموذج،
وتعديل إعداد الـhost، وتسجيل Skill، وإنشاء التخزين أو ترحيله، أو تغيير launcher profile فهي تعديلات
على البيئة وتتطلب صلاحية المشغّل/المستخدم المناسبة. إذا لم يملك الوكيل هذه الصلاحية، فعليه التوقف
عند الإرشاد وألا يدّعي أن تثبيتًا أو تهيئة قد حدثا.

استخدم [عمليات الإصدار](RELEASE_OPERATIONS.md) لتحديد إصدار FactLane المنشور الحالي. سجّل الـtag
الدقيق مع commit/tree أو artifact digests الموثقة هناك. لا تثبّت من فرع `main` المتغير، ولا تستنتج
هوية إصدار من package version داخل checkout عشوائي.

عند التثبيت من source ذي إصدار محدد، اضبط `FACTLANE_TAG` على الـtag المنشور المذكور في Release
operations، ثم استنسخ ذلك الـtag بالضبط:

```bash
FACTLANE_TAG='<published-tag-from-release-operations>'
git clone --branch "$FACTLANE_TAG" --depth 1 https://github.com/Habib1001-m/factlane.git
cd factlane
```

تحقق من commit/tree للـcheckout مقابل القيم المسجلة لذلك الـtag في Release operations قبل المتابعة.
إذا كنت تفضّل wheel أو source distribution منشورًا، فاتبع إجراء التثبيت المتحقق من الـdigest هناك
بدل تحويل مثال source-checkout بالتخمين.

ثبّت متطلبات Python و[`uv`](https://docs.astral.sh/uv/) و[Ollama](https://ollama.com/) التي يحددها
الإصدار المختار و[البيئة والتوافق](ENVIRONMENT.md). ثم تحقق من مكتبة SQLite المرتبطة بنفس بيئة
Python التي ستشغّل FactLane:

يتطلب ملف المنتج المدعوم حاليًا SQLite runtime مرتبطًا بإصدار **3.42.0+**. ومع ذلك تحقّق من manifest
الإصدار المختار وحدود Environment بدل افتراض أن إصدارًا مستقبليًا سيحتفظ بالحد الأدنى نفسه.

```bash
uv sync --frozen
uv run python -c 'import sqlite3; print(sqlite3.sqlite_version)'
uv run factlane --help
uv run factlane --help-tools
```

يستخدم فحص SQLite أعلاه مفسّر المشروع. قد يعرض executable باسم `python` أو `sqlite3` على مستوى
النظام إصدارًا *مختلفًا*. إذا كان runtime المرتبط أقل من الحد الأدنى المطلوب للإصدار المختار، يعيد
FactLane الخطأ `BACKEND_COMPATIBILITY_MISMATCH` قبل إنشاء قاعدة البيانات أو فتحها. إصدار Python
وحده لا يحقق عقد التخزين.

### افحص التخزين الموجود قبل التشغيل

اختر مسار قاعدة البيانات المقصود **قبل** بدء runtime الجديد. إذا لم توجد قاعدة بيانات في هذا
المسار، فتابع بإعداد جديد للقراءة فقط. إذا كان التخزين موجودًا بالفعل، فلا تربط runtime المثبّت
حديثًا به لمجرد نجاح تثبيت الحزمة. سجّل أولًا هوية deployment/runtime الحالية ومصدر التخزين، ثم
استخدم قواعد التوافق والانتقال الخاصة بالإصدار المختار في
[عمليات الإصدار](RELEASE_OPERATIONS.md) لتحديد ما إذا كان يمكن فتح قاعدة البيانات تلك مباشرة أو
أنها تتطلب ترحيلًا صريحًا.

إذا تعذر إثبات التوافق، فتوقف. وإذا أعاد فحص التوافق أو startup محكوم الحالة
`MIGRATION_REQUIRED`، فأوقف الخدمة العادية بدل محاولة تجاوز الـhold. احتفظ بنسخة احتياطية
متحقق منها ومملوكة للمشغّل وفق الإجراء الخاص بالإصدار، ثم اتبع تعليمات الترحيل والتراجع في
Release operations حرفيًا. لا تسمح لتشغيل MCP العادي بأن ينفذ ترحيل التخزين أو يخترع إجراءً له.

## 2. ثبّت نموذج تضمين مدعومًا

اختر embedding profile مدعومًا من [البيئة والتوافق](ENVIRONMENT.md)، وثبّت النموذج الدقيق الذي
يتطلبه ذلك الملف، ثم شغّل خدمة Ollama المحلية بالطريقة المعتادة لتثبيتك. لا تخمّن زوج
profile/model من مثال قديم. يجب أن تتطابق هوية النموذج وdigest والأبعاد وسعة الإدخال مع الملف
المختار؛ ولا يقوم FactLane بتنزيل نموذج أو استبداله تلقائيًا.

استخدم endpoint وحدود provider الموثقة للإصدار المختار. لا تضف remote embedding endpoint أو
fallback ما لم ينص ذلك الإصدار صراحة على دعمه.

## 3. اختر ملف تعريف التشغيل

يتطلب تشغيل خادم FactLane ثلاثة عناصر إعداد: `--db` لملف SQLite المحلي، و`--host-id` كتسمية
ثابتة غير سرية للـlauncher، وembedding `--profile`. اختر الملف من وثائق Environment الحالية
بدل الاعتماد على قيمة افتراضية ضمنية أو تاريخية.

للاتصال الأول، أبقِ الـlauncher للقراءة فقط بحذف `--write-profile`:

```bash
FACTLANE_PROFILE='<supported-profile-from-environment>'
uv run factlane \
  --db ./factlane.sqlite3 \
  --profile "$FACTLANE_PROFILE" \
  --host-id example-host
```

لا تشغّل هذا الأمر كأنه REPL؛ اضبط MCP host ليبدأه عبر stdin/stdout. وإذا كان الـhost يبدأ FactLane
من working directory آخر، فاستخدم مسارات **مطلقة** للـexecutable وقاعدة البيانات.

حذف `--write-profile` يبقي الاتصال الأول للقراءة فقط. لا تضف write profile قبل نجاح فحص الاتصال
للقراءة فقط في الخطوة 4.

### Codex (مضيف stdio مختبَر)

مثال لإدخال `~/.codex/config.toml`:

```toml
[mcp_servers.factlane]
command = "/absolute/path/to/factlane/.venv/bin/factlane"
args = [
  "--db", "/absolute/path/to/state/factlane.sqlite3",
  "--profile", "<supported-profile>",
  "--host-id", "codex"
]
enabled = true
```

أعد تحميل إعداد MCP في نسخة Codex المثبّتة لديك وتأكد من ظهور أدوات FactLane الخمس. ينبغي أن
يتبع استخدام الوكيل لهذه الأدوات [using-factlane Skill](USING_FACTLANE_SKILL.md)
المحمولة، والمسجّلة عبر آلية Skill التي يدعمها الـhost. يتضمن توزيع Skill الملف `SKILL.md`
والمرجع `references/host-bootstrap.md`. وجود الملفات يثبت وجود bytes الخاصة بالـSkill فقط؛ ولا
يثبت أن الـhost سجّلها أو اكتشفها أو حمّلها. أبقِ الحالات `present` و`registered`
و`discoverable` و`loaded` كحقائق منفصلة ملحوظة، واستخدم مرجع bootstrap للأدلة المطلوبة لكل
حالة.

### Hermes (مضيف stdio مختبَر)

مثال `~/.hermes/config.yaml`:

```yaml
mcp_servers:
  factlane:
    command: "/absolute/path/to/factlane/.venv/bin/factlane"
    args:
      - "--db"
      - "/absolute/path/to/state/factlane.sqlite3"
      - "--profile"
      - "<supported-profile>"
      - "--host-id"
      - "hermes"
```

أعد تحميل إعداد MCP في Hermes وتحقق من tool discovery. الـSkill المحمولة موجودة في source وpackage
artifacts الخاصة بـFactLane، لكنها **لا تُسجّل تلقائيًا** لدى أي host بمجرد تثبيت wheel. استخدم
آلية Skill التي يدعمها الـhost، وبلّغ `present` و`registered` و`discoverable` و`loaded`
كحالات منفصلة ملحوظة.

### عميل MCP آخر

اضبط executable نفسه، `factlane`، والوسائط نفسها في عميل يدعم MCP محليًا قائمًا على أوامر
**stdio**. امنح كل host موثوق تسمية `--host-id` ثابتة مناسبة. العملاء الآخرون متوافقون من حيث
البروتوكول من حيث المبدأ، لكن لم يتم تأهيلهم جميعًا على حدة. HTTP/SSE وStreamable HTTP غير
مدعومين في الخادم الحالي.

## 4. تحقق من الاتصال قبل الكتابة

ينبغي أن يكتشف MCP host الأدوات التالية بالضبط:

```text
memory_search
memory_get
memory_store
memory_update
memory_status
```

مجرد ظهور `memory_store` أو `memory_update` لا يمنح صلاحية استخدامهما؛ فما زال launcher
profile يتحكم في الكتابة. اطلب من العميل استدعاء `memory_status` ضمن نطاق دقيق. وللفحص في
GLOBAL_USER من دون الحاجة إلى project ID:

```json
{"scope":"GLOBAL_USER"}
```

إذا نجح الطلب، فقد اكتمل هدف التشغيل الأول الآمن: يستطيع الـhost تشغيل FactLane، ويظهر سطح الأدوات
العام، ويمكن لطلب للقراءة فقط عبور مسار runtime المضبوط.

### ملاحظات الهوية والاسترجاع

تصبح الأمثلة التالية مهمة عندما تتجاوز فحص الاتصال الأول.

في بحث PROJECT **غير مربوط**، قد يبدو الطلب هكذا:

```json
{
  "scope": "PROJECT",
  "project_id": "example-project",
  "intent_class": "CURRENT_PROJECT_STATE",
  "query": "What release constraints are recorded?",
  "retrieval_mode": "CURRENT",
  "retrieval_mode_kind": "KEYWORD"
}
```

عندما يكون الـhost الموثوق قد ربط بالفعل هوية project أو workflow، **احذف المعرّف المربوط المقابل
من الطلب**. يضيفه FactLane عند gateway ويرفض قيمة صريحة متعارضة باستخدام
`BOUND_CONTEXT_IDENTITY_MISMATCH`. يختلف `CROSS_PROJECT_WORKFLOW`: فهو يمنع وجود *كل* مفاتيح
هوية project وworktree وworkflow وagent، ولا يبحث في كل المشاريع تلقائيًا.

في بحث `CURRENT` الدلالي أو hybrid، يختار FactLane عبر مصادر provenance مخزنة ومتحقق منها
ومتميزة قبل ملء مواضع بمصادر مكررة. يبقى أعلى نتيجة مؤهلة محفوظًا، ولا يعاد كتابة relevance
scores المعادة، بينما تحافظ عمليات exact وkeyword-only و`REVIEW_HISTORY` على دلالات ترتيبها
الحالية.

تستخدم حالات الفشل المحكومة في FactLane قناة نتائج MCP نفسها المستخدمة للنجاح، وتعيد
`status=BLOCKED` مع `error_code` ثابت و`message` آمنة و`results` فارغة و
`audit.retryable`. تفرّع على `error_code` ولا تحلل نصوص الاستثناءات. وتظل الاستثناءات الداخلية
غير المتوقعة أخطاء transport لا نتائج FactLane محكومة.

## 5. فعّل كتابة Candidate فقط بعد نجاح القراءة فقط

العلم `--write-profile` هو **إعداد launcher موثوق**:

| Launcher profile | ما الذي يسمح به |
| --- | --- |
| محذوف / `read-only` | البحث والجلب والحالة؛ بلا تعديل للذاكرة. |
| `delegated-candidate` | يستطيع الوكيل العادي استخدام `memory_store` لإضافة `CANDIDATE`؛ ولا يستطيع التحقق من نفسه أو التحديث. |
| `owner-current`، `repo-verifier`، `automated-verifier` | ملفات مشغّل/متحقق موثوقة ومقيدة؛ لا تضبطها لوكيل عادي كي يحصل على صلاحية أعلى. |

موافقة المستخدم على المحتوى لا تغيّر صلاحية الوكيل العامل. ترقية Candidate هي عملية
`memory_update` موثوقة ومنفصلة. للموافقة على المحتوى مساران مدعومان: طلب صريح مثل «تذكّر هذا»،
أو حقيقة قابلة لإعادة الاستخدام يقترحها الوكيل ثم يصرح المستخدم بها صراحة. كلاهما يصرّح فقط
بمحتوى Candidate المحدود، وفق حدود runtime والخصوصية الفعالة؛ ولا يصرّح بتغيير launcher profile
أو التحول إلى verifier.

إذا كان يجب السماح للوكيل المتصل بإضافة **Candidates غير متحقق منها**، فأضف إعداد launcher
الموثوق:

```text
--write-profile delegated-candidate
```

يسمح هذا الملف لـ`memory_store` بإضافة Candidate، لكنه **لا** يسمح للوكيل العادي بالتحقق من
مساهمته أو ترقيتها.

إذا لم يحتج الاتصال إلى إضافة ذاكرة، فاترك write profile محذوفًا/للقراءة فقط.

إذا لم يُعد البحث نتيجة، فلا تعامل ذلك كإذن لاختراع حقيقة. لإضافة Candidate حقيقية، راجع
`factlane --help-tools` أو مخطط MCP الحي لمعرفة حقول `source_provenance` و`freshness_policy`
و`memory_type` و`idempotency_key` المطلوبة كاملة. استخدم حقيقة واحدة محدودة وقابلة للإسناد،
لا transcript أو dump عشوائي لمجلد. إذا كانت الحقيقة المقترحة من الوكيل وليست طلبًا صريحًا من
المستخدم، فاعرض الحقيقة المحدودة واحصل على تفويض صريح قبل استدعاء `memory_store`. لا تنفّذ
تخزينًا تلقائيًا بعد انتهاء الدور.

المتحقق الموثوق الذي يراجع Candidate يستخدم `REVIEW_HISTORY` لفحصها و`memory_update` مع
`mode=REVERIFY` و`expected_revision` و`expected_record_id` الخاص بالـCandidate لترقيتها.
لا يملك delegated agent صلاحية verifier حتى مع موافقة المستخدم في الدردشة. يقوم `REVERIFY`
بتحديث التحقق من دون تغيير contradiction identity للذاكرة؛ استخدم `REPLACE` بدل تغيير
`memory_type` أو subject عندما يكون المقصود إعادة تصنيف دلالية.

### مسار تحكم Owner موثوق اختياري

ينتهي مسار onboarding الطبيعي للوكيل عند أدوات MCP العامة الخمس أعلاه. قد تشحن بعض الإصدارات
المنشورة أسطح تحكم محلية للـOwner مثل `factlane-owner` أو `factlane-owner-webui`. تعامل معها
كـ**واجهات مشغّل موثوق اختيارية** فقط عندما يوثقها الإصدار المختار ويثبتها؛ ولا تفترض وجودها
من هذا البدء السريع.

لا ينشئ تحكم Owner أداة MCP سادسة، ولا يحوّل WebUI عبر loopback إلى HTTP MCP transport. اربط
عمليات Owner بالنطاق الموثوق الدقيق، واستخدم inventory/detail الحالية أو ملاحظات conflict لإثبات
revision وGroup CAS. لا تحاول عمليات lifecycle الخاصة بالـOwner من جلسة delegated-agent، ولا
تعتبر موافقة المحتوى في الدردشة صلاحية Owner أو صلاحية على مستوى الجهاز.

حيثما تكون مدعومة، فإن `REVOKED` و`REJECTED` حالتا lifecycle منطقيتان وليستا محوًا ماديًا.
Manual recheck وreadmission عمليتا Owner صريحتان، وليستا مراقبة مصدر في الخلفية أو database
restore أو إحياء لمحتوى أزيل عبر recovery. اتبع وثائق Owner والأمان الخاصة بالإصدار المختار
للحصول على الأوامر الدقيقة وحدود التعديل.

## 6. الحدود والمراجع التالية

تقتصر الحقائق على 2,000 UTF-8 byte. FactLane ليس raw-corpus indexer أو backup service أو remote
embedding gateway. إعادة إنتاج هذا الملف النموذجي لا تثبت جودة اللغة أو production-scale
throughput لبياناتك. Sensitive-memory recovery إجراء منفصل بتفويض من المشغّل، **وليس** أداة MCP
عامة؛ راجع [الأمان](SECURITY.md) لحدود الثقة الخاصة بالاستعادة.

تابع إلى [أدوات MCP الخمس](TOOLS.md) للحصول على دليل الأدوات الموجه للمطورين.

للتقييم الأعمق، راجع [المعمارية](ARCHITECTURE.md) لتدفق البيانات الدقيق،
و[البيئة](ENVIRONMENT.md) لبيئة التشغيل والملفات،
و[عمليات الإصدار](RELEASE_OPERATIONS.md) للتثبيت/الترقية/التراجع المرتبط بالإصدار،
و[الأمان](SECURITY.md) لحدود الثقة والاستعادة. ويظل مخطط MCP الحي ومخرجات
`--help-tools` المرجع النهائي لتواقيع الطلبات والقيم المعدودة.
