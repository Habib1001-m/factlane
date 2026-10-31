# البدء السريع

يشرح هذا الدليل إعداد خادم FactLane MCP محلي وربطه بوكيل. الخادم عبارة عن
**عملية `stdio` يبدأها مضيف MCP**، وليس خدمة ويب أو تطبيقًا تفاعليًا في الطرفية.
وهو يخزّن حقائق محدودة، وليس أداة لاستيراد المحادثات.

## كيف يبدو النجاح

في الإعداد الأول، اجعل الهدف محدودًا. تكون قد انتهيت عندما:

1. يستطيع مضيف MCP المتوافق تشغيل FactLane؛
2. تظهر أدوات FactLane الخمس بالضبط؛
3. ينجح طلب `memory_status` للقراءة فقط ضمن نطاق دقيق.

ابدأ بوضع القراءة فقط. لا تفعّل كتابة Candidate إلا بعد نجاح الاتصال الأساسي.

## 1. ثبّت بيئة التشغيل وتحقق منها

ثبّت `Python 3.11+` و[`uv`](https://docs.astral.sh/uv/) و[Ollama](https://ollama.com/)
على الجهاز الذي سيشغّل FactLane. يجب أن تكون بيئة Python التي يستخدمها FactLane مرتبطة بإصدار
**`SQLite 3.42.0+`**.

```bash
git clone --branch v0.1.4 --depth 1 https://github.com/Habib1001-m/factlane.git
cd factlane
uv sync --frozen
uv run python -c 'import sqlite3; print(sqlite3.sqlite_version)'
uv run factlane --help
uv run factlane --help-tools
```

يستخدم فحص SQLite أعلاه مفسّر المشروع. وقد يعرض `python` أو `sqlite3` المثبّت على مستوى النظام
إصدارًا *مختلفًا*. إذا كان الإصدار المرتبط أقل من الحد الأدنى، يُرجع FactLane
`BACKEND_COMPATIBILITY_MISMATCH` قبل إنشاء قاعدة البيانات أو فتحها. إصدار Python
وحده لا يفي بعقد التخزين.

يتعمد أمر النسخ تثبيت إصدار `v0.1.4` بدلًا من فرع `main` المتغير.
للحصول على بصمات الملفات، وتثبيت الحزم المنشورة، والترقيات، والتراجع، استخدم
[عمليات الإصدار](RELEASE_OPERATIONS.md).

### تقييم خط المصدر/RC الحالي 0.1.5

يعرّف مصدر المستودع الحالي نفسه باسم `0.1.5`، لكن لا توجد بعد هوية إصدار `v0.1.5` منشورة.
إذا كنت تقيّم checkout محليًا للـRC جرى تزويدك به، فاستخدم ذلك checkout الدقيق بدل اختراع أو
نسخ tag باسم `v0.1.5`:

```bash
uv sync --frozen
uv run python -c 'from importlib.metadata import version; print(version("factlane"))'
uv run factlane --help-tools
uv run factlane-owner --help
uv run factlane-migrate-storage-v3 --help
```

ينبغي أن يبلغ هذا المصدر عن package `0.1.5` وPublic Contract Revision 3 وعن أدوات MCP الخمس
نفسها المذكورة أدناه. وهو يستخدم عقد التخزين v3. وإذا وجهته إلى قاعدة storage-v2، يفشل بدء التشغيل
العادي في وضع مغلق مع `MIGRATION_REQUIRED`؛ استخدم إجراء الترحيل الصريح في
[عمليات الإصدار](RELEASE_OPERATIONS.md) مع نسخة pre-v3 متحقق منها قبل الخدمة العادية.

## 2. ثبّت نموذج تضمين مدعومًا

يستخدم المثال التالي ملف التعريف المضمّن `embeddinggemma-300m-768`:

```bash
ollama pull embeddinggemma:300m
```

شغّل خدمة Ollama المحلية بالطريقة المعتادة لتثبيتك. اتصال FactLane الافتراضي هو
`http://127.0.0.1:11434`. يجب أن تتطابق هوية النموذج وبصمته وأبعاده وسعة الإدخال
مع ملف التعريف؛ ولا يتم تنزيل نموذج أو استبداله تلقائيًا.

ملفات التعريف المضمّنة الأخرى، بما فيها بدائل Nomic، موضحة في
[البيئة والتوافق](ENVIRONMENT.md). تعتمد ملاءمة النموذج على أحجام حقائقك، ومزيج اللغات،
ومتطلبات زمن الاستجابة، والعتاد. النموذج المحدد في هذا المثال ليس توصية عامة.
لا يدعم FactLane حاليًا نقاط نهاية تضمين بعيدة.

## 3. اختر ملف تعريف التشغيل

يتطلب تشغيل خادم FactLane ثلاثة عناصر إعداد: `--db` (ملف SQLite المحلي)، و`--host-id`
(معرّف ثابت غير سري للمشغّل)، و`--profile` للتضمين (قيمته الافتراضية `nomic-768`،
لذلك عيّنه صراحة عند استخدام نموذج المثال).

في الاتصال الأول، أبقِ المشغّل في وضع القراءة فقط بحذف `--write-profile`:

```bash
uv run factlane \
  --db ./factlane.sqlite3 \
  --profile embeddinggemma-300m-768 \
  --host-id example-host
```

لا تشغّل هذا الأمر على أنه REPL؛ اضبط مضيف MCP ليشغّله عبر stdin/stdout.
إذا كان المضيف يبدأ FactLane من مجلد عمل آخر، فاستخدم مسارات **مطلقة** للملف التنفيذي
ولقاعدة البيانات.

إغفال `--write-profile` يُبقي هذا الاتصال الأول للقراءة فقط. لا تضف ملف تعريف كتابة
حتى ينجح فحص اتصال القراءة فقط في الخطوة 4.

### Codex (مضيف `stdio` مختبَر)

مثال على إدخال `~/.codex/config.toml`:

```toml
[mcp_servers.factlane]
command = "/absolute/path/to/factlane/.venv/bin/factlane"
args = [
  "--db", "/absolute/path/to/state/factlane.sqlite3",
  "--profile", "embeddinggemma-300m-768",
  "--host-id", "codex"
]
enabled = true
```

أعد تحميل إعداد MCP في إصدار Codex المثبّت لديك وتأكد من ظهور أدوات FactLane الخمس.
ينبغي أن يتبع استخدام الوكيل لهذه الأدوات Skill المحمول
[`using-factlane`](USING_FACTLANE_SKILL.md)، المثبّت عبر آلية Skill التي يدعمها مضيفك. إصدار
`v0.1.4` المحدد في الخطوة 1 يشحن `SKILL.md` و`references/host-bootstrap.md`. المرجع الثاني
محايد للمضيف للفحص والفصل بين حالات التسجيل وتقرير الأدلة؛ ولا يعني وجوده أن المضيف سجّل Skill
أو حمّلها. لا تفترض أن Skill مسجّلة أو
discoverable أو loaded لمجرد وجود ملفاتها.

### Hermes (مضيف `stdio` مختبَر)

مثال على `~/.hermes/config.yaml`:

```yaml
mcp_servers:
  factlane:
    command: "/absolute/path/to/factlane/.venv/bin/factlane"
    args:
      - "--db"
      - "/absolute/path/to/state/factlane.sqlite3"
      - "--profile"
      - "embeddinggemma-300m-768"
      - "--host-id"
      - "hermes"
```

أعد تحميل إعداد Hermes وتحقق من اكتشاف الأدوات. Skill المحمول مضمن في مصدر FactLane
وحزمته، لكنه **لا يُسجَّل تلقائيًا** مع أي من المضيفين بمجرد تثبيت wheel.
استخدم آلية Skill التي يدعمها المضيف، وأبلغ عن `present` و`registered` و`discoverable`
و`loaded` كحالات مرصودة منفصلة.

### عميل MCP آخر

اضبط الملف التنفيذي `factlane` والحجج نفسها في عميل يدعم MCP محليًا قائمًا على الأوامر
عبر **`stdio`**. امنح كل مضيف موثوق `--host-id` ثابتًا مناسبًا. العملاء الآخرون متوافقون
مع البروتوكول من حيث المبدأ، لكن لم تُؤهَّل جميعها على حدة.
لا يدعم الخادم الحالي HTTP/SSE ولا streamable HTTP.

## 4. تحقق من الاتصال قبل الكتابة

ينبغي أن يكتشف مضيف MCP بالضبط:

```text
memory_search
memory_get
memory_store
memory_update
memory_status
```

مجرد *ظهور* `memory_store` أو `memory_update` لا يمنح صلاحية؛ إذ يظل ملف تعريف المشغّل
هو المتحكم في الكتابة. اطلب من العميل استدعاء `memory_status` ضمن نطاق دقيق.
لفحص حالة على مستوى global-user لا يتطلب معرّف مشروع:

```json
{"scope":"GLOBAL_USER"}
```

إذا نجح هذا الطلب، يكون هدف التشغيل الأول الآمن قد اكتمل: يستطيع المضيف تشغيل FactLane،
وتظهر واجهة الأدوات العامة، ويمكن لطلب للقراءة فقط عبور مسار بيئة التشغيل المضبوطة.

### ملاحظات الهوية والاسترجاع

تهم الأمثلة التالية عندما تتجاوز فحص الاتصال الأول.

لبحث غير مرتبط مسبقًا ضمن نطاق مشروع، قد يبدو الطلب هكذا:

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

عندما يكون مضيفك الموثوق قد ربط بالفعل هوية مشروع أو سير عمل، **احذف المعرّف المرتبط المقابل
من الطلب**. يضيفه FactLane عند البوابة ويرفض معرّفًا صريحًا متعارضًا باستخدام
`BOUND_CONTEXT_IDENTITY_MISMATCH`. يختلف `CROSS_PROJECT_WORKFLOW`: فهو يمنع وجود *كل*
مفاتيح هوية المشروع وworktree وسير العمل والوكيل، ولا يبحث تلقائيًا في كل المشاريع.

في بحث `CURRENT` الدلالي أو الهجين، يختار FactLane عبر مصادر منشأ (provenance) مخزنة ومتحقق منها
قبل ملء الخانات من المصادر المتكررة. تُحفَظ أعلى نتيجة مؤهلة في الترتيب، ولا تُعاد كتابة درجات
الصلة المُعادة، كما تحتفظ أنماط الاسترجاع exact وkeyword-only و`REVIEW_HISTORY` بدلالات ترتيبه الحالية.

تستخدم إخفاقات FactLane المحكومة قناة نتائج MCP نفسها التي تستخدمها الاستدعاءات الناجحة،
وتعيد `status=BLOCKED` مع `error_code` ثابت، و`message` آمنة، و`results` فارغة،
و`audit.retryable`. فرّع المنطق بناءً على `error_code`؛ ولا تستخرج المعنى من نص الاستثناء.
تظل الاستثناءات الداخلية غير المتوقعة أخطاء نقل بدل إعادة تصنيفها كنتائج محكومة من FactLane.

## 5. فعّل كتابة Candidate فقط بعد نجاح القراءة فقط

الخيار `--write-profile` هو إعداد **موثوق للمشغّل**:

| ملف تعريف المشغّل | ما الذي يسمح به |
| --- | --- |
| محذوف / `read-only` | البحث والجلب والحالة؛ دون تعديل للذاكرة. |
| `delegated-candidate` | يستطيع الوكيل العادي استخدام `memory_store` للمساهمة بسجل `CANDIDATE`؛ ولا يستطيع التحقق منه ذاتيًا أو تحديثه. |
| `owner-current`, `repo-verifier`, `automated-verifier` | ملفات تعريف مقيدة للمشغّل/المتحقق الموثوق؛ لا تضبطها لوكيل عادي ليكتسب صلاحية أكبر. |

موافقة المستخدم على المحتوى لا تغيّر صلاحيات الوكيل العامل. ترقية Candidate هي عملية
`memory_update` موثوقة ومنفصلة. لموافقة المحتوى مساران مدعومان: طلب صريح مثل «تذكّر هذا»،
أو حقيقة قابلة لإعادة الاستخدام يقترحها الوكيل ثم يفوضها المستخدم صراحة. كلاهما يصرح فقط
بمحتوى Candidate المحدود ضمن حد runtime/privacy الفعّال؛ ولا يصرح بتغيير launcher profile
أو باكتساب سلطة المتحقق.

إذا كان يجب السماح للوكيل المتصل بالمساهمة بسجلات Candidate **غير متحقق منها**، فأضف إعداد المشغّل الموثوق:

```text
--write-profile delegated-candidate
```

يسمح ملف التعريف هذا لـ`memory_store` بالمساهمة بسجل Candidate. لكنه **لا** يسمح للوكيل العادي
بالتحقق من مساهمته أو ترقيتها بنفسه.

إذا كان الاتصال لا يحتاج إلى المساهمة بذاكرة، فاترك ملف تعريف الكتابة محذوفًا/`read-only`.

إذا لم يُرجع البحث نتيجة، فلا تعتبر ذلك إذنًا لاختراع حقيقة. للمساهمة بسجل Candidate فعلي،
راجع `factlane --help-tools` أو مخطط MCP الحي للحصول على الحقول المطلوبة كاملة:
`source_provenance` و`freshness_policy` و`memory_type` و`idempotency_key`.
استخدم حقيقة واحدة محدودة وقابلة للإسناد، وليس نص محادثة أو تفريغًا اعتباطيًا لمجلد.
إذا كانت الحقيقة المقترحة قد نشأت من الوكيل بدل طلب مستخدم صريح، فاعرض الحقيقة المحدودة
واحصل على تفويض صريح قبل استدعاء `memory_store`. ولا تنفّذ تخزينًا ذاتيًا بعد انتهاء الدور.

يستخدم متحقق موثوق يراجع Candidate وضع `REVIEW_HISTORY` لفحصه، ثم يستخدم
`memory_update` مع `mode=REVERIFY` و`expected_revision` و`expected_record_id` الخاص
بسجل Candidate لترقيته. لا يمتلك الوكيل المفوض صلاحية المتحقق، حتى مع موافقة المستخدم في المحادثة.
يجدّد `REVERIFY` التحقق من دون تغيير هوية التناقض للذاكرة؛ استخدم `REPLACE` بدل تغيير
`memory_type` أو `subject` عندما تكون إعادة تصنيف دلالية مطلوبة.

### دورة حياة Owner وإعادة الفحص اليدوية في خط المصدر/RC 0.1.5

يحافظ Public Contract Revision 3 على عدد أدوات MCP عند خمس أدوات، مع إضافة وضعي
`REVOKE`/`REJECT` بصلاحية Owner مباشرة إلى `memory_update`. وللمراجعة التشغيلية، تُعرض
`OwnerControlService` المحلية عبر `factlane-owner`. وتشمل أوامر القراءة `inventory` و`get` و
`conflicts` و`status`، بينما تشمل أوامر التعديل `recheck` و`readmit` و`reverify` و`replace` و
`revoke` و`reject` و`resolve`.

اربط Owner CLI بالنطاق الموثوق الدقيق في وسيطات المشغّل. وقدّم دليل التعديل عبر ملف JSON محدود
أو stdin حتى لا يحتاج الدليل إلى الظهور داخل وسيطات سطر الأوامر. على سبيل المثال، بعد أن يعيد
inventory/detail مراجعة السجل الدقيقة وtoken مجموعة التناقض:

```bash
cat > revoke.json <<'JSON'
{
  "memory_id": "11111111-1111-4111-8111-111111111111",
  "expected_record_id": "22222222-2222-4222-8222-222222222222",
  "expected_revision": 3,
  "observed_group_token": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "idempotency_key": "owner-revoke-example-1",
  "reason_code": "OUTDATED"
}
JSON

uv run factlane-owner \
  --db ./factlane.sqlite3 \
  --host-id local-owner \
  --scope PROJECT \
  --project-id example-project \
  revoke --input revoke.json
```

الـtoken والمعرّفات في المثال placeholders؛ استخدم فقط القيم العائدة من مراجعة Owner الحالية
الدقيقة. `REVOKED` و`REJECTED` حالتان منطقيتان نهائيتان وليستا حذفًا ماديًا.

ولا تبدأ سياسة حداثة `on_change` watcher تلقائيًا للمصدر. ينفّذ Owner أمر `recheck` صريحًا بدليل
حديث ومحدود. قد يجدّد الرصد unchanged الأهلية؛ أما changed أو unavailable أو indeterminate فيفشل
في وضع مغلق للمراجعة. و`readmit` عملية Owner منفصلة لذاكرة منطقية مؤهلة في `REVOKED` مع تحقق جديد
وGroup CAS. وهي ليست استعادة لقاعدة البيانات ولا تعيد إنشاء محتوى أزالته استعادة الذاكرة الحساسة.

يُرفق مصدر 0.1.5 أيضًا launcher محليًا لـOwner WebUI مدعومًا من `OwnerControlService` نفسها:

```bash
uv run factlane-owner-webui \
  --db ./factlane.sqlite3 \
  --host-id local-owner \
  --scope PROJECT \
  --project-id example-project
```

يرتبط launcher بدقة عبر loopback `127.0.0.1` ويطبع كائن READY JSON واحدًا يحتوي `origin` الفعلي،
ومسار `/owner/`، ومسار `/owner/bootstrap`، واسم bootstrap header، وbootstrap token للاستخدام مرة
واحدة، والنطاق المربوط. استخدم الـtoken المطبوع فقط داخل header باسم `X-FactLane-Bootstrap` في
`POST <origin>/owner/bootstrap`. **لا** تضف الـtoken إلى URL ولا تخزنه في `localStorage`. عند نجاح
التبادل يُستهلك الـtoken، ويضبط الخادم session cookie من نوع `HttpOnly; SameSite=Strict`، ويعيد
CSRF token؛ وتتطلب تعديلات Owner اللاحقة الجلسة الموثقة وCSRF header. يظل الـWebUI سطح Owner محليًا،
وليس نقل MCP عبر HTTP، ولا يضيف أداة MCP ولا ينشئ أي هوية v0.1.5 منشورة أو deployed.

## 6. الحدود والمراجع التالية

الحقائق محدودة إلى **2,000 UTF-8 bytes**. FactLane ليس أداة فهرسة لمجموعة بيانات خام، ولا خدمة نسخ احتياطي،
ولا بوابة تضمين بعيدة. إعادة إنتاج النتائج على ملف تعريف المثال هذا لا تثبت جودة اللغة أو معدل
المعالجة على نطاق الإنتاج لبياناتك. استعادة الذاكرة الحساسة إجراء منفصل ومصرح به للمشغّل،
وهو **ليس** أداة MCP عامة؛ استخدم [الأمان](SECURITY.md) لحدود الثقة الخاصة بالاستعادة.

تابع إلى [أدوات MCP الخمس](TOOLS.md) للاطلاع على دليل الأدوات الموجّه للمطورين.

للتقييم الأعمق، راجع [البنية](ARCHITECTURE.md) لمسار البيانات الدقيق،
و[البيئة](ENVIRONMENT.md) لبيئة التشغيل وملفات التعريف،
و[عمليات الإصدار](RELEASE_OPERATIONS.md) للتثبيت والترقية والتراجع حسب الإصدار،
و[الأمان](SECURITY.md) لحدود الثقة والاستعادة. يظل مخطط MCP الحي ومخرجات
`--help-tools` المرجع المعتمد لتواقيع الطلبات وقيم enums.
