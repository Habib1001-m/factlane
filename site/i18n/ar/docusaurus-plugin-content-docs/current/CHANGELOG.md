# سجل تغييرات FactLane

يسجل هذا الـchangelog التغييرات الظاهرة للمستخدم ومتطلبات الترقية لكل إصدار. ولإجراءات التثبيت
والترحيل والتراجع وهوية الإصدار الدقيقة، استخدم
[عمليات الإصدار](RELEASE_OPERATIONS.md).

## 0.1.7 (Unreleased)

FactLane v0.1.7 هو هوية حزمة التطوير الحالية في شجرة المصدر هذه. وهو **ليس إصدارًا منشورًا** في حالة التوثيق هذه؛ فلا تستنتج tag أو GitHub Release أو نشرًا على PyPI أو انتقالًا لموقع Production من رقم الإصدار وحده. يظل خط أساس الحزمة المنشور المسجل أدناه v0.1.6 إلى أن يسجل إصدار مستقل ومصرح به هوية ثابتة جديدة.

### أبرز تغييرات التطوير

- يوسّع دعم Windows x64/AMD64 الأصلي من تكافؤ وقت التشغيل العادي إلى دورة حياة Owner Panel المُدارة، بما يشمل التحكم في الخدمة للمستخدم الحالي، وHTTPS المحلي، وتكامل Firefox المعزول، وإدارة قاعدة البيانات، والاستعادة الكاملة المؤهلة، والمحو الدائم المقيد بالإمكانات المتاحة.
- يضيف تنفيذ ترحيل storage-v2→v3 وتراجع دقيق إلى ما قبل v3 على Windows x64/AMD64 الأصلي. وهو منفصل عن مشغّل Recovery العام لحوادث الذاكرة الحساسة، الذي يبقى عقدًا مقصورًا على POSIX.
- تبدأ Owner Panel المُدارة الآن Memories باكتشاف Owner-wide للقراءة فقط وReview بقائمة Candidates على مستوى Owner، مع بقاء إثبات التعديل مربوطًا بالنطاق الدقيق. يستخدم اعتماد Candidate تحكم تاريخ/وقت محليًا مع إظهار منطقة المتصفح الزمنية، بينما تبقى تفاصيل record/revision/Group-CAS داخل المسار العادي. تعرض **Activity** سجل العمليات ودورة الحياة المحلي القابل للتصفية؛ وتوجد Diagnostics المحلية والمنقحة داخل الجلسة والإعدادات.
- يضيف `factlane launch webui` كتركيب طبيعي لـstatus/start/open بعد الإعداد. يثبت الإعداد الناجح مشغّل Linux `.desktop` بلا Terminal أو اختصار Start Menu للمستخدم الحالي على Windows يعيد استخدام مسار الفتح الموثوق نفسه.
- يضيف `factlane doctor webui` للقراءة فقط لاكتشاف runtime/interpreter وربط قاعدة البيانات/profile المُدار والـSkill المضمّن وحالة launcher/service ووصفة MCP محايدة للمضيف بوضع read-only، مع بقاء هوية MCP host والنطاق صريحين. ويمكن لإعداد Linux المُدار في التشغيل الأول الآن إكمال ربط ذاكرة غائب عبر setup shell الموثق، مع تحقق دقيق من التخزين/profile، ومن دون ترحيل ضمني، ومع نشر متحقق منه للإعداد الخاص بالمالك وإعادة تشغيل مُدارة.
- تظل الاستعادة الكاملة والمحو الدائم تتطلبان معاينة دقيقة وموافقة منفصلة عبر آلية المنصة الأصلية على الإجراء الحساس قبل التنفيذ.
- يمكن استئناف جلسة Owner مُدارة صالحة بعد إعادة تحميل الصفحة مع تدوير CSRF من دون إعادة ضبط مهلة الخمول أو العمر المطلق.
- يقتصر تشغيل المتصفح المُدار على Windows على Firefox، باستخدام ملف FactLane مخصص والتحقق من الجاهزية ضمن حدود محددة قبل تسليم محدد اتصال لمرة واحدة. تجاوز شهادات المتصفح وإعادة استخدام ملف شخصي وإعادة تسليم المحدد ليست وسائل استعادة مدعومة.
- يشدّد مزوّد Ollama المرفق بحيث تظل وجهات طلبات HTTP الخاصة بـFactLane عند نقطة نهاية بـloopback literal دقيق: تُعطّل وراثة إعدادات proxy المحيطة وتُرفض إعادة التوجيه.
- يصحح صياغة مساعدة/مخطط `memory_update` الحي بحيث توثق عمليتا Owner `REVOKE`/`REJECT` متطلب `expected_record_id` الدقيق، إضافة إلى إثبات Group CAS والسبب.

### الترقية وحالة الإصدار

يظل عقد التخزين v3. لا تتطلب قاعدة رُحلت بالفعل إلى v3 ترحيل مخطط جديدًا لمجرد الانتقال من وقت تشغيل v0.1.6 إلى شجرة تطوير v0.1.7 الحالية. يجب على تثبيت ما زال يستخدم storage v2 تشغيل مشغّل الترحيل/التراجع الصريح المدعوم على المنصة المستهدفة قبل الخدمة العادية. راجع [عمليات الإصدار](RELEASE_OPERATIONS.md).

لا يعيّن هذا القسم غير المنشور هوية إصدار عامة.

## 0.1.6

FactLane v0.1.6 هو خط أساس حزمة Python المنشور المسجل في هذا المستودع. وهو corrective patch release فوق GitHub
release المختوم v0.1.5: تستخدم الحزمة الآن اعتماد registry دقيقًا ومتوافقًا مع PyPI
`mcp-memory-service==11.10.0` بدل direct VCS requirement الذي منع قبول wheel الخاصة بـv0.1.5 على
PyPI. ويظل Public Contract Revision 3 وstorage contract v3 ونقل `stdio` المشغّل بالأمر وسطح MCP
العام المكوّن من خمس أدوات بالضبط دون تغيير.

على Python 3.11 أو أحدث، مسار التثبيت العادي من PyPI هو:

```bash
pip install factlane==0.1.6
```

ولبيئة Python 3.11 معزولة مع فحص صريح للاعتمادات بعد التثبيت:

```bash
python3.11 -m venv .venv
.venv/bin/python -m pip install "factlane==0.1.6"
.venv/bin/python -m pip check
.venv/bin/factlane --help
```

يحل مسار PyPI هذا اعتماد backend المباشر الدقيق `mcp-memory-service==11.10.0` من package index.
وهو مسار تثبيت الحزمة المباشر، لكنه لا يعيد تشغيل كل transitive dependency محفوظة في lockfile
بصورة مطابقة. استخدم tagged source checkout مع `uv sync --frozen` عندما تكون إعادة تشغيل lockfile
بدقة مطلوبة.

### نطاق الإصدار التصحيحي

- يستبدل direct VCS dependency metadata غير المتوافقة مع PyPI بالـregistry pin الدقيق
  `mcp-memory-service==11.10.0`.
- يحافظ على fail-closed backend identity عبر ربط bytes الخاصة بوقت تشغيل registry الذي تمت مراجعته
  وعقد class/signature المطلوب للـbackend.
- يبقي Public Contract Revision 3 وstorage contract v3 و`stdio` والأدوات العامة الخمس دون تغيير.
- لا يرحّل durable data ولا يغيّر tag/assets الخاصة بـv0.1.5 ولا يرفع إصدار Production للموقع العام
  الذي يُدار كسطح نشر منفصل.

### هوية الإصدار

| العنصر | هوية v0.1.6 |
| --- | --- |
| Package version | `0.1.6` |
| Annotated tag | `v0.1.6` |
| Tag object | `75cf56de9cb693ca07f9d750fc08ff22e0f70e0d` |
| Release commit | `e4a71aedaaf4f2e73aeaeaafbdeca60a846d21ad` |
| Release tree | `4664d232810da749c2267fa56dc1568ab2af1f71` |
| Wheel | `factlane-0.1.6-py3-none-any.whl` |
| Wheel SHA-256 | `5cb74af31e6bfe96bc38e0048ea2e8abcde53c5a05470aebbe9ed8305dd46899` |
| Source distribution | `factlane-0.1.6.tar.gz` |
| Source distribution SHA-256 | `cb755605126cbc7f2fcc7c4335e020aa9f6da67f0812dcd7622d99e18c53b84b` |
| Public Contract Revision | `3` |
| Storage contract | `3` |
| Public MCP transport | `stdio` |
| Public MCP tools | `memory_search`, `memory_get`, `memory_store`, `memory_update`, `memory_status` |
| PyPI publication | منشور باسم `factlane==0.1.6` |
| Site Production deployment | سطح publication منفصل؛ لم يتقدم بسبب هذا package release |

يظل trusted-publish workflow run الذي رفع v0.1.6 أحمر تاريخيًا لأن فحص **ما بعد الكتابة** لظهور
PyPI انتهت مهلته بعد نجاح خطوة الرفع. وأثبتت readback مستقلة بعد ذلك أن PyPI يقدم بالضبط bytes
الخاصة بالـwheel والـsource distribution المؤهلتين أعلاه. لذلك لا يمثل لون الـrun فشلًا في رفع
الحزمة، ولا ينبغي إعادة تشغيله لمجرد جعل الحالة خضراء.

## 0.1.5

FactLane v0.1.5 هو GitHub release تاريخي مختوم مع Public Contract Revision 3 وstorage contract v3.
وتظل هويات tag وrelease assets المسجلة الخاصة به دليل الإصدار التاريخي، ولا يجوز retarget أو
استبدالها لإعادة كتابة ذلك التاريخ. لكن نشره على PyPI لم يكتمل: رفض PyPI direct
VCS dependency metadata الموجودة في wheel، ولا يوجد package version 0.1.5 منشور على PyPI.
ويُعد ذلك الفشل terminal؛ فـv0.1.6 هو successor تصحيحي متوافق مع PyPI، وليس mutation أو retry
للإصدار v0.1.5.

### أبرز التغييرات

- يضيف Owner control plane محكومًا لمراجعة الذاكرة الدائمة والعمل عليها من دون توسيع سطح MCP
  العام خارج الأدوات الخمس الحالية.
- يضيف disposition مباشرًا من Owner عبر `REVOKE` و`REJECT`، وحماية compare-and-swap لمجموعة
  التناقض، وسجلًا دائمًا لأحداث/عمليات lifecycle، وreadmission محكومًا.
- يضيف انتقالًا صريحًا من storage-v2 إلى storage-v3 مع backup متحقق منه قبل v3 ومسارًا محددًا
  للتراجع عن البيانات. ولا تُرقّى قواعد storage-v2 الموجودة بصمت أثناء startup العادي.
- يضيف إعادة فحص يدوية لحداثة `on_change` بنتائج مرتبطة بالدليل. ولا يضيف FactLane watcher
  تلقائيًا للمصدر.
- يرفق Owner CLI محليًا وWebUI عبر loopback مع إبقاء نقل MCP عبر `stdio` يُشغَّل بالأمر فقط.

### ما أُضيف

- `OwnerControlService` للفحص المحدود للمخزون والتفاصيل، ومراجعة التعارضات، وحالة الصحة
  والاحتفاظ، وإجراءات lifecycle، وإعادة الفحص اليدوية للحداثة، وreadmission.
- `factlane-owner`، وهو Owner CLI موثوق ومرفق. تربط launcher arguments نطاق Owner وهوية الـhost؛
  ولا يمكن لحمولة mutation استبدال تلك السلطة المربوطة.
- `factlane-owner-webui`، وهو Owner WebUI محلي مرفق ومربوط بالضبط إلى `127.0.0.1` وإلى نطاق
  واحد غير قابل للتغيير. يصدر startup bootstrap token قصير العمر للاستخدام مرة واحدة ولا يُتبادل
  إلا عبر header `X-FactLane-Bootstrap`. وتتطلب mutations الموثقة session الناتجة وCSRF token.
  والـWebUI سطح عرض للـOwner، لا HTTP MCP transport ولا remote-admin service.
- `factlane-migrate-storage-v3`، وهو مشغّل الترحيل/الاستعادة الموثوق والصريح للانتقال من v2 إلى v3.
- readmission محكوم لذاكرة منطقية مؤهلة في `REVOKED` بعد تحقق جديد وفحوص CAS حالية. ينشئ
  readmission successor محكومًا؛ وليس backup restore ولا يستطيع إعادة إنشاء محتوى أزاله
  sensitive-memory Recovery.
- سجلات دائمة لأحداث lifecycle وعمليات lifecycle لقرارات حوكمة storage-v3.

### ما تغيّر

- يتقدم Public Contract Revision من 2 إلى 3. وتبقى مجموعة أدوات MCP العامة بالضبط:
  `memory_search` و`memory_get` و`memory_store` و`memory_update` و`memory_status`.
- تدعم `memory_update` الآن `REVOKE` و`REJECT` بصلاحية Owner مباشرة إلى جانب `REVERIFY`
  و`REPLACE`. تتطلب dispositions الخاصة بالـOwner رقم revision الحالي الدقيق وملاحظة مجموعة التناقض
  وحماية idempotency وreason code محدودًا.
- يتقدم storage contract من v2 إلى v3. ويضيف storage v3 حواجز writer محكومة وGroup CAS لمجموعة
  التناقض وlifecycle history وحماية idempotency عامة عبر adapter writes العادية وعمليات Owner lifecycle.
- `REVOKED` و`REJECTED` حالتا lifecycle منطقيتان نهائيتان. ولا تمثلان محوًا ماديًا لمحتوى قاعدة
  البيانات.
- يمكن الآن إعادة فحص حداثة `on_change` عبر عملية Owner صريحة. قد تجدد ملاحظة unchanged الأهلية؛
  بينما يفشل الدليل changed أو unavailable أو indeterminate أو stale أو mismatched أو غير المقبول
  مغلقًا للمراجعة.

### ترقية مطلوبة: من storage v2 إلى v3

ترقية قاعدة v0.1.4 دائمة هي **ترحيل بيانات/schema**، لا تحديث package فقط. يفحص startup العادي
في 0.1.5 قاعدة موجودة ويعيد `MIGRATION_REQUIRED` عند storage v2. لا تربط runtime 0.1.5 بهذه
القاعدة وتتوقع منه ترقية schema تلقائيًا.

قبل الترحيل:

1. أوقف FactLane أو ضعه في quiescence ليحصل مشغّل الترحيل على maintenance access حصري.
2. اختر مسار backup جديدًا داخل مجلد private موجود ويملكه مشغّل الترحيل.
   يجب ألا يمنح parent الخاص بالـbackup صلاحيات للمجموعة أو للآخرين (`mode & 077 == 0`).
3. احتفظ بهوية runtime/source الحالية لـv0.1.4 حتى يكتمل الترحيل والتحقق بعده.

نفّذ الترحيل فقط على maintenance host مدعوم من POSIX:

```bash
uv run factlane-migrate-storage-v3 migrate \
  --db /absolute/path/to/factlane.sqlite3 \
  --backup /absolute/path/to/backups/factlane-pre-v3.sqlite3
```

يتحقق المشغّل من storage v2 الدقيق وlineage، وينشئ ويتحقق من backup وverification manifest
المملوكين للمشغّل بدلالات private/no-replace، ثم يطبق governance schema v3 transactionally ويرفع
contract version في النهاية. احتفظ بمسار backup وSHA-256 المبلغ عنهما كدليل rollback.

إذا كان يجب التخلي عن حالة v3 قبل قبولها كمرجعية دائمة، فأوقف FactLane واستعد backup الدقيق
والمتحقق منه قبل v3 عبر مشغّل الترحيل:

```bash
uv run factlane-migrate-storage-v3 restore \
  --db /absolute/path/to/factlane.sqlite3 \
  --backup /absolute/path/to/backups/factlane-pre-v3.sqlite3
```

تثبيت أو إعادة تشغيل v0.1.4 مباشرة على قاعدة storage-v3 ليس downgrade مدعومًا للبيانات. راجع
[عمليات الإصدار](RELEASE_OPERATIONS.md) لعقد الترحيل والتراجع الكامل.

### الأمان

- ترتبط قرارات lifecycle الخاصة بالـOwner المباشر بسلطة launcher موثوقة وحالة revision الدقيقة
  وGroup CAS لمجموعة التناقض وidempotency وreason codes محدودة. يظل delegated agent العادي
  Candidate-only ولا يستطيع ترقية نفسه بتغيير حقول الطلب.
- يضيف storage v3 writer fencing محليًا لكل connection وفحوص idempotency عامة حتى لا يستطيع
  writer قديم/غير موثوق أو إعادة استخدام المفتاح عبور فئات mutation المحكومة بصمت.
- الـOwner WebUI يعمل عبر loopback فقط ويستخدم bootstrap exchange لمرة واحدة وcookie session
  من نوع HttpOnly; SameSite=Strict وفحوص origin/host وحماية CSRF للـmutations.
- تلقت تبعيات Python/runtime وsite-toolchain تحديثات أمان خلال دورة 0.1.5. وهذا ليس ادعاء
  zero-advisory: لا تزال سلسلة build الحالية في Docusaurus 3.10.2 تحمل advisories متبقية
  upstream-blocked في build/dev/preview dependencies. هذه الحزم جزء من site toolchain المؤهل
  بصورة منفصلة؛ وشكل الموقع production الذي تمت مراجعته static output وليس Node/Docusaurus
  application server. يظل خطر build-time وsupply-chain المتبقي قرار إصدار ولا يُمثّل على أنه
  عولج.
- يظل sensitive-memory Recovery مسار صيانة منفصلًا ومصرحًا به للمشغّل. ولا تستبدل
  `REVOKE`/`REJECT` المنطقية وreadmission ضوابط الاستعادة المادية للذاكرة الحساسة.

### التوافق

- يظل متطلب Python هو `>=3.11` ويظل الحد الأدنى لـSQLite المرتبط هو `>=3.42.0`.
- يظل نقل MCP عبر `stdio` يُشغَّل بالأمر فقط، مع خمس أدوات عامة بالضبط.
- يستمر دعم native Windows x64/AMD64 **لوقت التشغيل العادي** الذي أُنشئ لـv0.1.4. ولا يوسع ذلك
  حدود الصيانة: يظل sensitive-memory Recovery وstorage-v2 إلى v3 migration/restore POSIX-only.
  ولا يوجد ادعاء بدعم Windows ARM64.
- تظل قواعد embedding-space compatibility الموجودة في v0.1.4 منفصلة عن schema migration الجديد
  في storage-v3. نجاح تثبيت package وحده لا يثبت توافق البيانات الدائمة.
- تظل Python package والموقع العام سطحَي إصدار منفصلين. لا تتضمن wheel/source distribution
  المؤهلة الموقع، ولا يؤدي تأهيل الموقع أو نشره إلى نشر Python package.

### الحدود المعروفة

- لا يراقب `on_change` المصادر تلقائيًا. يجب أن يقدّم Owner موثوق ملاحظة حديثة ومحدودة لإعادة
  الفحص اليدوية.
- الـOwner WebUI سطح مشغّل محلي عبر loopback، وليس remote administration مدعومًا ولا HTTP MCP server.
- `REVOKED`/`REJECTED` حالتا نهاية منطقيتان؛ ولا تدعيان الحذف المادي.
- يقتصر readmission على ذاكرة `REVOKED` المؤهلة ولا يستطيع استعادة محتوى أزيل ماديًا بواسطة
  sensitive-memory Recovery.
- storage-v2 إلى v3 migration/restore وsensitive-memory Recovery عمليات صيانة POSIX-only.
- تظل semantic relevance وجودة اللغة وسلوك filesystem خارج الملفات المؤهلة وحجم deployment
  خاصة بعبء العمل والبيئة بدل أن تكون ضمانات عامة.

### هوية الإصدار

| العنصر | هوية v0.1.5 |
| --- | --- |
| Package version | `0.1.5` |
| Public Contract Revision | `3` |
| Storage contract | `3` |
| Public MCP transport | `stdio` |
| Public MCP tools | `memory_search`, `memory_get`, `memory_store`, `memory_update`, `memory_status` |
| Annotated tag | `v0.1.5` |
| Tag object | `e4275fb33147c3336797bb230312699571a0afe5` |
| Release commit | `eaa75f8f17961cbaaf94d5d623e5a80083dad56f` |
| Release tree | `6cc017b508571be0636f6f4b031aa4ca452893c4` |
| Wheel | `factlane-0.1.5-py3-none-any.whl` |
| Wheel SHA-256 | `9ff8039125cab109c169dacc62a88aae91cbbe2cd12abb0c2661e72db84c4a79` |
| Source distribution | `factlane-0.1.5.tar.gz` |
| Source distribution SHA-256 | `7f997ecb85a519e5c0b44d136a774182f1453d832dda02a795463602d627a0ac` |
| PyPI publication | غير منشور؛ رفض terminal لـdirect-VCS metadata |
| Site Production deployment | سطح publication منفصل؛ لا تتضمنه هذه package release ضمنيًا |

## 0.1.4

حافظ FactLane v0.1.4 على Public Contract Revision 2 وعلى سطح MCP نفسه ذي الأدوات الخمس، وأضاف
مسار embedding-space compatibility المؤهل من v0.1.3، وتأهيل native Windows x64 لوقت التشغيل
العادي، وإرشادات bootstrap للـSkill المحمولة، وضوابط package/release أقوى، وسطح publication ثنائي
اللغة. وتظل هوية الإصدار التاريخية الدقيقة وملاحظات الترحيل في
[عمليات الإصدار](RELEASE_OPERATIONS.md).

## 0.1.3

كان FactLane v0.1.3 أول إصدار إنتاج رسمي. وتظل هويته التاريخية الدقيقة وحدود دعمه في
[عمليات الإصدار](RELEASE_OPERATIONS.md).
