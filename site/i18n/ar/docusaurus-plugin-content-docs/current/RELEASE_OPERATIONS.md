# عمليات الإصدار: التثبيت والترقية والتراجع وإعادة التحقق

يحدد دليل التشغيل هذا الإجراء العام للمشغّل لاستخدام إصدار محدد من FactLane
ولتخطيط الانتقالات اللاحقة بين الإصدارات. تتكون هوية الإصدار من الوسم ذي الإصدار مع
الـ commit/tree المتحقق منهما، والقيم المسجلة لملخصات artifacts المنشورة لذلك الوسم.
ولصيانة الإصدارات، يشترط هذا الدليل ألا تُعاد توجيه الوسوم المنشورة بصمت وألا تُستبدل
الأصول المنشورة لجعل وثائق لاحقة تتفق مع bytes مختلفة. يمكن لأذونات المنصة تقنيًا تعديل
هذه الكائنات، لذلك تحقّق من الهويات بدلًا من الثقة في الاسم وحده. وقد يتلقى فرع `main`
تغييرات في الوثائق أو التطوير بعد الإصدار، ولذلك يجب ألا يُستخدم بديلًا عن هوية إصدار
تاريخية.

لملخص تغييرات الإصدار والتطوير الموجّه للمستخدم وتأثير الترقية، راجع
[سجل التغييرات](CHANGELOG.md). ويظل هذا الدليل المرجع المعتمد لهوية الإصدار الدقيقة وإجراءات
الترحيل والتراجع والتثبيت وإعادة التحقق بعد التثبيت.

### هوية حزمة Development

خط أساس حزمة Python المنشور المسجل في هذا المستودع هو `v0.1.6`. يعلن مصدر التطوير الحالي `0.1.7` في `pyproject.toml`، لكن البناء المحلي من فرع تطوير ليس تلقائيًا حزمة إصدار رسمية. تتكون هوية الإصدار المسجلة من annotated tag وcommit/tree الدقيقين وملفات GitHub Release المنشورة وبيانات الحزمة المقابلة المسجلة أدناه. إذا احتجت إلى معرفة ما إذا كان إصدار أحدث موجودًا خارجيًا، فتحقق منه بصورة منفصلة قبل تغيير التثبيت؛ ولا تستنتجه من شجرة المصدر المتغيرة هذه.

يصنّف CI الخاص بالمستودع هذا الحد بواسطة `scripts/check_release_surface.py`. ويكون تصنيف
Development مغلقًا عمدًا أمام هوية الإصدار: فهو يسجل أن المصدر يختلف عن الوسم المنشور، ولا
يعيّن صلاحية نشر أو صلاحية Production. ولا يؤدي إعادة استخدام version string منشور في commit
لاحق على `main` إلى ضم ذلك الـtree إلى الإصدار التاريخي. ويجب أن يحصل أي إصدار مستقبلي على tag
وartifact digests مخولة بصورة منفصلة قبل اعتباره حقيقة إصدار منشورة.

## عقد هوية الإصدار

لكل إصدار رسمي، سجّل كل ما يلي قبل التثبيت أو الترقية:

- الإصدار والوسم annotated tag؛
- الـ commit وGit tree اللذان يصل إليهما ذلك الوسم؛
- أسماء wheel وsource distribution المنشورة وملخصات SHA-256 الخاصة بها؛
- متطلبات Python وSQLite المرتبط؛
- Public Contract Revision ومجموعة أدوات MCP العامة الدقيقة؛
- أي متطلبات لترحيل البيانات/schema وعقد التراجع الخاص بها؛
- الحدود أو قيود التأهيل الخاصة بالإصدار.

لا تستنتج التوافق من رقم الإصدار وحده. توافق الحزمة، وتوافق وقت التشغيل،
وتوافق قاعدة البيانات/schema هي ادعاءات منفصلة.

## Manifest الخاص بـ FactLane v0.1.3

`v0.1.3` هو أول إصدار إنتاج رسمي من FactLane. وهويته المنشورة هي:

| العنصر | هوية v0.1.3 |
| --- | --- |
| Annotated tag | `v0.1.3` |
| Tag object | `d922fcceae7cfbfda239666182e61a5a58278809` |
| Release commit | `59173b7d9fa3af924c373176411882caa04a6c1d` |
| Release tree | `93db940802432b6f9ca2f5513916fd2b84c6393a` |
| Wheel | `factlane-0.1.3-py3-none-any.whl` |
| Wheel SHA-256 | `daf145cd7754b7c46a0f18ad49cc8872291094c8ac84621fce3cec091f90072a` |
| Source distribution | `factlane-0.1.3.tar.gz` |
| Source distribution SHA-256 | `81de371ec23d17109f9267750fed80cd6492cee245681d72630da6868f3cd16b` |
| CPython | `>=3.11` |
| Linked SQLite | `>=3.42.0` |
| Public Contract Revision | `2` |
| Public MCP tools | `memory_search`, `memory_get`, `memory_store`, `memory_update`, `memory_status` |
| Portable Skill SHA-256 | `1c28bc370af0dd89499f8121fb7b611afb4c83773207ce546136abd8018f3e24` |

أصول الإصدار منشورة في
[GitHub Release for `v0.1.3`](https://github.com/Habib1001-m/factlane/releases/tag/v0.1.3).
تحقّق من bytes التي نزّلتها بمقارنتها بالملخصات أعلاه قبل التثبيت.

### حدود دعم v0.1.3

بالنسبة إلى `v0.1.3`، يتمثل الملف المدعوم الموثق في MCP محلي من نوع stdio يُشغَّل
بالأوامر، وPython 3.11+، وSQLite مرتبط 3.42.0+، وتضمينات Ollama محلية مدعومة،
وعقد التخزين/الاستعادة المحلي الموثق على POSIX.

لا يثبت الإصدار جودة دلالية لكل اللغات، ولا سلوكًا اعتباطيًا لعملاء MCP أو نظام الملفات،
ولا مقياس نشر اعتباطيًا. تظل الصلة الدلالية للعربية/المحتوى مختلط اللغات خاصة بعبء العمل.
وقد اختُبر استنفاد سعة SQLite بصورة حتمية وسلوك القراءة فقط بسبب الأذونات؛ أما حالة
kernel `ENOSPC` حقيقية وmount `EROFS` حقيقي فلم تُختبرا.

`v0.1.3` هو أيضًا أول إصدار رسمي. لا يوجد إصدار إنتاج رسمي أقدم يستطيع FactLane
تسميته هدف تراجع إنتاجي مدعومًا. وقدرة مثبت الحزم على استبدال إصدار بآخر ليست دليلًا
على أن وقت تشغيل أقدم يمكنه فتح قاعدة بيانات عدّلها إصدار أحدث بأمان.

## ملاحظات إصدار FactLane v0.1.4 والانتقال من v0.1.3

`v0.1.4` هو الإصدار اللاحق المعتمد لـ`v0.1.3`. يحافظ على Public Contract Revision 2 وعلى الأدوات
العامة الخمس نفسها، ويضيف مسار توافق/ترحيل مؤهلاً لفضاء embeddings في v0.1.3، وتأهيل بيئة
Windows x64 القياسية، وإرشادات bootstrap للـSkill، وضوابط أقوى للحزم والإصدار والنشر، وسطح النشر ثنائي اللغة. يظل Pi مقيدًا بادعاء تكامل مسار القراءة على Windows الأصلي المقبول سابقًا؛ ولا يضاف ادعاء أوسع بأنه مضيف للكتابة.

### الترحيل من v0.1.3

- احتفظ بنسخة احتياطية يملكها المشغّل قبل فتح البيانات الدائمة بواسطة v0.1.4.
- يمكن للفضاء المعروف `embeddinggemma-300m-768` من v0.1.3 أن ينتقل metadata-only فقط بعد نجاح
  إثبات التوافق المقيّد بالسلامة؛ لا تُعاد كتابة المتجهات أو provenance المسجل لكل record.
- الهوية المجهولة أو المختلطة أو غير المتوافقة تبقى fail-closed للبحث semantic/hybrid والكتابات
  التي تغيّر المتجهات، مع بقاء مسارات get/exact/keyword الآمنة كما هو موثق.
- يظل Public Contract Revision هو `2` وتظل الأدوات العامة الخمس `memory_search` و`memory_get`
  و`memory_store` و`memory_update` و`memory_status` دون تغيير.
- تظل sensitive-memory recovery عملية للمشغّل فقط وPOSIX-only؛ تأهيل Windows لا يوسع هذا الحد.
- الرجوع بالحزمة/وقت التشغيل إلى v0.1.3 لا يضمن خفض البيانات الدائمة. لا تفتح بيانات عُدلت تحت v0.1.4 باستخدام v0.1.3 إلا إذا ثبت توافق حالة البيانات المعنية أو استُعيدت من نسخة احتياطية متحقق منها تسبق الترقية.

هوية v0.1.4 المنشورة مختومة ومسجلة الآن كما يلي:

| العنصر | هوية v0.1.4 |
| --- | --- |
| Annotated tag | `v0.1.4` |
| Tag object | `4276f3cbae14ebce89030d516f64682aabae59c9` |
| Release commit | `4b2cf5b27d454799dd253c0306d83bd076c9369f` |
| Release tree | `d9a69de5e63419bfb6aade745e25137e07857cbb` |
| Wheel | `factlane-0.1.4-py3-none-any.whl` |
| Wheel SHA-256 | `b1a256d124d74ffd6b9dba4338c171e35b95fa901e232bb1ed59ebd4f0316307` |
| Source distribution | `factlane-0.1.4.tar.gz` |
| Source distribution SHA-256 | `80c01dbf20dd5139350a9434c787c27a468f458397740fd0d85064394be1f5a3` |
| Python | `>=3.11` |
| Linked SQLite | `>=3.42.0` |
| Public Contract Revision | `2` |
| Public MCP tools | `memory_search`, `memory_get`, `memory_store`, `memory_update`, `memory_status` |
| Portable Skill `SKILL.md` SHA-256 | `6255ddb8eeea074cd324d1f096d3e043f09a189b46cb3f64c724bb29ecd397a4` |
| Portable Skill `references/host-bootstrap.md` SHA-256 | `05882a0d6716e32cd23ef452c1683a8aff1294abfcc7950fea771320cd211d4c` |

هذه حقائق إصدار تاريخية. ولا تمنح أي هوية أو صلاحية نشر لأي إصدار لاحق.

## إصدار FactLane v0.1.5 والانتقال من v0.1.4

FactLane v0.1.5 هو GitHub release تاريخي مختوم مع package version `0.1.5` وPublic Contract
Revision 3 وstorage contract v3. وتظل هويات tag وGitHub Release assets المسجلة الخاصة به دليلًا
تاريخيًا، ولا يجوز retarget أو استبدالها لإعادة كتابة ذلك التاريخ. لكن نشر PyPI لم يكتمل لأن PyPI رفض direct VCS dependency metadata في wheel؛ ولذلك لا يتوفر
v0.1.5 باسم `factlane==0.1.5` من PyPI. يصلح successor v0.1.6 مشكلة package-distribution metadata
فقط. تلخص [فقرة 0.1.5 في سجل التغييرات](CHANGELOG.md) التغييرات الظاهرة للمستخدم، ويحدد هذا القسم
آليات الانتقال.

يحافظ الإصدار على نقل MCP العام عبر **stdio فقط** وعلى سطح MCP العام عند الأدوات الخمس نفسها بالضبط.
ويوسع Revision 3 أداة `memory_update` بوضعي `REVOKE`/`REJECT` بصلاحية Owner مباشرة، ويضيف سطح
تحكم Owner المحكوم حول دورة الحياة المخزنة نفسها: `OwnerControlService` وCLI المرفق
`factlane-owner` وlauncher المرفق `factlane-owner-webui` عبر loopback. يطبع launcher الـorigin
المربوط وbootstrap token للاستخدام مرة واحدة؛ ويُتبادل الـtoken فقط عبر header
`X-FactLane-Bootstrap` ولا يوضع في URL أو `localStorage`. والـWebUI طبقة عرض محلية للـOwner فوق
الخدمة، وليس خادم MCP عبر HTTP ولا أداة MCP سادسة.

يضيف storage v3 حالتي `REVOKED` و`REJECTED` المنطقيتين النهائيتين، وGroup CAS لمجموعة التناقض،
وسجلات دائمة لأحداث وعمليات lifecycle، وإعادة فحص يدوية لحداثة `on_change`، وreadmission محكومًا.
دورة الحياة النهائية منطقية ولا تدعي المحو المادي. والـreadmission عملية دورة حياة متحققة لذاكرة
مؤهلة في `REVOKED`، وليست استعادة backup ولا إحياءً بعد sensitive-Recovery.

ويظل `on_change` معتمدًا على الدليل بدل المراقبة الآلية. لا يشغّل الإصدار watcher في الخلفية يعيد
التحقق من المصدر. يقدم Owner ملاحظة حديثة ومحدودة لعملية إعادة الفحص اليدوية؛ والدليل changed أو
unavailable أو indeterminate أو stale أو mismatched يفشل مغلقًا للمراجعة.

هوية v0.1.5 المختومة هي:

| العنصر | هوية v0.1.5 |
| --- | --- |
| Annotated tag | `v0.1.5` |
| Tag object | `e4275fb33147c3336797bb230312699571a0afe5` |
| Release commit | `eaa75f8f17961cbaaf94d5d623e5a80083dad56f` |
| Release tree | `6cc017b508571be0636f6f4b031aa4ca452893c4` |
| Wheel | `factlane-0.1.5-py3-none-any.whl` |
| Wheel SHA-256 | `9ff8039125cab109c169dacc62a88aae91cbbe2cd12abb0c2661e72db84c4a79` |
| Source distribution | `factlane-0.1.5.tar.gz` |
| Source distribution SHA-256 | `7f997ecb85a519e5c0b44d136a774182f1453d832dda02a795463602d627a0ac` |
| Python | `>=3.11` |
| Linked SQLite | `>=3.42.0` |
| Public Contract Revision | `3` |
| Storage contract | `3` |
| Public MCP tools | `memory_search`, `memory_get`, `memory_store`, `memory_update`, `memory_status` |
| PyPI | غير منشور؛ رفض terminal لـdirect-VCS metadata |

### ترحيل التخزين المطلوب من v2 إلى v3

لا يرحّل بدء v0.1.5 العادي قاعدة v0.1.4/storage-v2 بصمت. يفشل runtime مغلقًا مع
`MIGRATION_REQUIRED` حتى يكمل مشغّل الترحيل الموثوق الانتقال. تعامل مع ذلك باعتباره **ترحيل
بيانات/schema** لا ترقية حزمة فقط. نجاح تثبيت الحزمة لا يصرّح بفتح بيانات storage-v2 بواسطة
runtime 0.1.5 العادي. أوقف FactLane أو ضعه في quiescence،
واختر مسار backup جديدًا داخل **مجلد private موجود مسبقًا ويملكه مشغّل الترحيل**. يجب ألا يمنح
مجلد الـbackup أي صلاحيات للمجموعة أو للآخرين (`mode & 077 == 0`)؛ ولا تنشئ عملية الترحيل هذا
المجلد أو توسّع صلاحياته نيابة عن المشغّل:

كلٌّ من عملية الترحيل هذه ومسار restore المقابل لنسخة pre-v3 هو **عملية صيانة POSIX-only**.
ولا يشمل دعم Windows القياسي لوقت التشغيل ترحيل v2 → v3 أو restore.

```bash
uv run factlane-migrate-storage-v3 migrate \
  --db /absolute/path/to/factlane.sqlite3 \
  --backup /absolute/path/to/backups/factlane-pre-v3.sqlite3
```

يتطلب المشغّل storage v2 دقيقًا، ويتحقق من integrity وlineage، وينشئ SQLite backup أولًا داخل inode
staging سري بصلاحية `0600` في المجلد نفسه، ثم يعيد فتح الـinode الموجود دون السماح بإعادة إنشائه،
ويتحقق من integrity والعقد v2، ثم ينشر الـbackup ذريًا دون استبدال أي مسار موجود. ويُنشر manifest
التحقق بدلالات private/no-replace مماثلة. تُرفض أي وجهة backup أو manifest موجودة مسبقًا. وإذا نُشر
الـbackup بالفعل ثم فشل أو انقطع نشر الـmanifest، تُحتفظ النسخة المتحقق منها وتتوقف عملية الترحيل قبل
تغيير قاعدة المصدر؛ وعلى المشغّل التحقيق بدل الكتابة فوقها. وبعد وجود الـbackup والـmanifest المتحقق
منهما، يطبّق المشغّل مخطط الحوكمة v3 transactionally، ويرفع إصدار عقد التخزين في النهاية، ثم يتحقق
من قاعدة الناتج كـv3.

بعد الترحيل، احتفظ بمسار backup وSHA-256 المبلغ عنهما كدليل rollback، وأعد تشغيل فحوص وقت التشغيل
العادية قبل تفعيل الكتابة.

يغيّر الترحيل عقد التخزين الدائم الخاص بـPython runtime فقط. ولا ينشر Python package ولا ينشر
الموقع العام ولا يمنح أيًا من السطحين صلاحية Production. يظل تأهيل/نشر الموقع مسار publication
منفصلًا عن نشر wheel/source distribution.

### rollback للبيانات من v3 إلى v2

إذا كان يجب التراجع عن الترحيل قبل قبول حالة v3 كمرجعية دائمة، فأوقف/جمّد وقت تشغيل v0.1.5
واستعد نسخة pre-v3 الدقيقة والموثقة عبر مشغّل الترحيل:

```bash
uv run factlane-migrate-storage-v3 restore \
  --db /absolute/path/to/factlane.sqlite3 \
  --backup /absolute/path/to/backups/factlane-pre-v3.sqlite3
```

يتحقق restore من أن backup هو storage contract v2، ويجهّز صورة الاستبدال وينفذ fsync لها، ويرقيها
تحت maintenance lease، ويزيل WAL/SHM sidecars القديمة، ثم يتحقق من قاعدة البيانات المستعادة كـv2.
ولا ينبغي للمشغّل التفكير في إعادة تشغيل v0.1.4 إلا بعد إثبات rollback للبيانات. تثبيت v0.1.4
مباشرة فوق قاعدة storage-v3 ليس downgrade مدعومًا.

## إصدار FactLane v0.1.6 التصحيحي للحزمة

يحافظ FactLane v0.1.6 على سلوك المنتج وعقود storage/public في v0.1.5، مع تصحيح package dependency
metadata التي منعت نشر PyPI. أصبح اعتماد backend المباشر هو registry requirement الدقيق
`mcp-memory-service==11.10.0`؛ وتظل bytes الخاصة بوقت التشغيل التي تمت مراجعتها وتوافق
class/signature المطلوب مربوطين بسياسة fail-closed.

هوية v0.1.6 المختومة هي:

| العنصر | هوية v0.1.6 |
| --- | --- |
| Annotated tag | `v0.1.6` |
| Tag object | `75cf56de9cb693ca07f9d750fc08ff22e0f70e0d` |
| Release commit | `e4a71aedaaf4f2e73aeaeaafbdeca60a846d21ad` |
| Release tree | `4664d232810da749c2267fa56dc1568ab2af1f71` |
| Wheel | `factlane-0.1.6-py3-none-any.whl` |
| Wheel SHA-256 | `5cb74af31e6bfe96bc38e0048ea2e8abcde53c5a05470aebbe9ed8305dd46899` |
| Source distribution | `factlane-0.1.6.tar.gz` |
| Source distribution SHA-256 | `cb755605126cbc7f2fcc7c4335e020aa9f6da67f0812dcd7622d99e18c53b84b` |
| Python | `>=3.11` |
| Linked SQLite | `>=3.42.0` |
| Public Contract Revision | `3` |
| Storage contract | `3` |
| Public MCP tools | `memory_search`, `memory_get`, `memory_store`, `memory_update`, `memory_status` |
| PyPI | منشور باسم `factlane==0.1.6` |

### تثبيت v0.1.6 من PyPI

على CPython 3.11 أو أحدث، مسار package index العام المدعوم مباشر ولا يحتاج Git dependency checkout:

```bash
pip install factlane==0.1.6
```

ولبيئة CPython 3.11 معزولة وفحص للاعتمادات والـCLI:

```bash
python3.11 -m venv .venv
.venv/bin/python -m pip install "factlane==0.1.6"
.venv/bin/python -m pip check
.venv/bin/factlane --help
```

على Native Windows عبر PowerShell تُستخدم هوية الحزمة نفسها مع مسارات البيئة الخاصة بـWindows:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install "factlane==0.1.6"
.\.venv\Scripts\python.exe -m pip check
.\.venv\Scripts\factlane.exe --help
```

أُعيد التحقق من هذا المسار من public PyPI index بعد الإصدار: يثبت FactLane `0.1.6` ويحل
`mcp-memory-service==11.10.0` ويمرر `pip check` ويكشف CLI المرفق. وهو package install وليس إعادة
تشغيل دقيقة لكل transitive version في repository lockfile. لإعادة تشغيل الاعتمادات بدقة، استخدم
tagged source checkout لـv0.1.6 مع `uv sync --frozen` وتحقق من tag/commit/tree أعلاه.

الانتقال إلى v0.1.6 من حالة storage-v3/v0.1.5 المقبولة هو package/runtime-only؛ ولا يضيف schema
migration جديدة. أما deployment الذي ما زال على v0.1.4/storage-v2 فيجب أن يكمل عقد الترحيل v2 →
v3 أعلاه قبل استخدام runtime v0.1.6 العادي مع قاعدة البيانات الدائمة تلك.

الموقع العام سطح إصدار منفصل. نشر v0.1.6 إلى PyPI لم يرفع هوية Production للموقع ولم يصرح بنشره.

## انتقال تطوير FactLane v0.1.7 (غير منشور)

يعلن المصدر الحالي `0.1.7`، لكن هذا التوثيق لا يعيّن هوية ثابتة لوسم v0.1.7 أو حزمه أو نشره على PyPI أو الموقع. تعامل مع هذا القسم بوصفه إرشادًا للتطوير والتشغيل، وليس بيان إصدار منشور.

يظل عقد التخزين v3. لا تحتاج قاعدة storage-v3 مقبولة بالفعل من v0.1.5/v0.1.6 إلى انتقال مخطط جديد لمجرد الانتقال إلى بيانات تطوير v0.1.7 الحالية. أما التثبيت الذي ما زال على storage v2 الدقيق فيجب أن يكمل الترحيل الصريح v2→v3 قبل خدمة v0.1.7 العادية.

### ترحيل v2 → v3 على Windows x64/AMD64 الأصلي

بخلاف تنفيذ ترحيل v0.1.5 التاريخي أعلاه، تتضمن شجرة تطوير 0.1.7 الحالية تنفيذ ترحيل/تراجع مستقلًا على Windows x64/AMD64 الأصلي. أوقف الخدمة المُدارة واستخدم مسارات أقراص Windows أصلية لقاعدة البيانات والنسخة الاحتياطية. لا تستخدم مسارات WSL أو UNC.

إذا ظل FactLane أو أي process آخر يمسك قاعدة البيانات النشطة أو ملفات WAL/SHM الخاصة بها بصورة غير متوافقة، يفشل المشغّل مغلقًا قبل تعديل storage النشط. أغلق أو أوقف الـholder ثم أعد الأمر؛ ولا تحذف أو تستبدل قاعدة البيانات أو sidecars لتجاوز هذا التوقف.

مثال PowerShell من بيئة تطوير حالية مثبّتة:

```powershell
factlane-migrate-storage-v3 migrate `
  --db "C:\path\to\state\factlane.sqlite3" `
  --backup "C:\path\to\backup\factlane-pre-v3.sqlite3"
```

يتحقق مشغّل Windows من storage v2 الدقيق وحيازة الهدف، ويحافظ على العزل المستمر طوال نافذة تعديل قاعدة البيانات النشطة وWAL/SHM، وينشئ نسخة خاصة قبل v3 ويتحقق منها مع بيان التحقق الخاص بها قبل تغيير storage النشط، ويثبت حالة rollback متحققًا منها قبل تغيير قاعدة البيانات النشطة، ثم يتحقق من صورة v3 الناتجة قبل تحرير حيازة الصيانة. لا تُستبدل وجهات النسخ الاحتياطي أو بيانات التحقق الموجودة بصمت.

يستخدم التراجع النسخة الدقيقة المتحقق منها قبل v3 وحالة التحقق المربوطة بها:

```powershell
factlane-migrate-storage-v3 restore `
  --db "C:\path\to\state\factlane.sqlite3" `
  --backup "C:\path\to\backup\factlane-pre-v3.sqlite3"
```

يعيد مسار الاستعادة قاعدة البيانات النشطة إلى الصورة المتحقق منها قبل v3 ويفحص النتيجة قبل تحرير حيازة الصيانة. لا يكون exact rollback مدعومًا إلا ما دام الهدف هو نتيجة v3 المباشرة للترحيل والمربوطة بتلك النسخة. أي تغييرات v3 منطقية/lifecycle/trust-clock مقبولة لاحقًا أو عدم تطابق في ربط النسخة بالهدف يفشل مغلقًا مع `ROLLBACK_HOLD` من دون تغيير البيانات. التراجع عن الحزمة وحده ليس تراجعًا عن البيانات الدائمة.

إذا انقطع migrate أو restore، فاترك قاعدة البيانات والنسخة الاحتياطية وملفات التحقق كما هي، ثم أعد العملية نفسها بالمسارات نفسها. لا تستبدل أو تحذف هذه الملفات يدويًا؛ يستأنف المشغّل من الحالة المتحقق منها عندما يمكن إثبات ذلك، أو يفشل مغلقًا إذا تعذر إثبات استئناف آمن.

عقد ترحيل/تراجع Windows هذا منفصل عن مسار الاستعادة الكاملة لقاعدة البيانات في Owner Panel المُدارة وعن Recovery العامة لحوادث الذاكرة الحساسة. تبقى الأخيرة عقد `SensitiveMemoryRecoveryOperator` المنفصل والمقصور على POSIX.

### Owner Panel المُدارة

توفر شجرة تطوير 0.1.7 الحالية أيضًا دورة الحياة المُدارة `factlane ... webui` على Windows x64/AMD64 الأصلي. راجع [البدء السريع](QUICKSTART.md) لمسار status/start/open و[الأمان](SECURITY.md) لحدود الثقة الخاصة بالجلسة والموافقة على الإجراء الحساس والمحو الدائم والاستعادة الكاملة. لا تعيّن هذه الإمكانات المحلية هوية إصدار عامة لـv0.1.7.

## تثبيت إصدار دقيق

انسخ الهوية الدقيقة من manifest الإصدار الذي اخترته إلى متغيرات shell قبل استخدام أي من
مساري التثبيت. لا تستبدلها بقيم من `main` المتحرك أو قائمة package index أو الذاكرة:

```bash
RELEASE_TAG="<annotated tag>"
RELEASE_COMMIT="<release commit>"
RELEASE_TREE="<release tree>"
WHEEL_NAME="<wheel filename>"
WHEEL_SHA256="<wheel SHA-256>"
SDIST_NAME="<source-distribution filename>"
SDIST_SHA256="<source-distribution SHA-256>"
```

### الخيار A: versioned source checkout

استخدم وسم الإصدار ذي النسخة، وتحقق من commit/tree الخاصين به، بدل الاعتماد على
فرع `main` المتحرك:

```bash
git clone --branch "$RELEASE_TAG" --depth 1 https://github.com/Habib1001-m/factlane.git
cd factlane

test "$(git rev-parse HEAD)" = "$RELEASE_COMMIT"
test "$(git rev-parse 'HEAD^{tree}')" = "$RELEASE_TREE"

uv sync --frozen
uv run python -c 'import sqlite3; print(sqlite3.sqlite_version)'
uv run factlane --help
uv run factlane --help-tools
```

### الخيار B: wheel المنشورة

نزّل wheel الخاصة بالإصدار، وتحقق منها، ثم ثبّتها في بيئة جديدة:

```bash
curl -fLO "https://github.com/Habib1001-m/factlane/releases/download/${RELEASE_TAG}/${WHEEL_NAME}"
printf '%s  %s\n' "$WHEEL_SHA256" "$WHEEL_NAME" | sha256sum -c -

python3.11 -m venv .venv
.venv/bin/python -m pip install "./${WHEEL_NAME}"
.venv/bin/python -m pip check
.venv/bin/factlane --help
.venv/bin/factlane --help-tools
```

تسجل wheel متطلبات FactLane المباشرة، لكن مصدر الاعتماد خاص بكل release: لا تفترض أن الإصدار
المحدد يستخدم Git source أو package-index source من دون مراجعة metadata وملاحظات ذلك الإصدار.
وقد يحل مثبت جديد إصدارات أحدث متوافقة من الاعتمادات **transitive** مقارنة بالبيئة التي يلتقطها
lockfile في المستودع. عندما تكون إعادة تشغيل الاعتمادات بدقة مهمة، استخدم source checkout الموسوم
مع `uv sync --frozen` وملف `uv.lock` المحفوظ معه.

يُعد source distribution أيضًا سطح clean-install مدعومًا بعد التحقق من SHA-256 الخاص به:

```bash
curl -fLO "https://github.com/Habib1001-m/factlane/releases/download/${RELEASE_TAG}/${SDIST_NAME}"
printf '%s  %s\n' "$SDIST_SHA256" "$SDIST_NAME" | sha256sum -c -

python3.11 -m venv .venv
.venv/bin/python -m pip install "./${SDIST_NAME}"
```

### أوامر PowerShell المكافئة على Windows الأصلي

للتحقق من إصدار على Windows x64/AMD64 الأصلي، استخدم قيم manifest نفسها بدل إعادة تكوينها من الذاكرة. يمكن لـsource checkout موسوم التحقق من commit/tree الدقيقين في PowerShell:

```powershell
$ReleaseTag = '<annotated tag>'
$ReleaseCommit = '<release commit>'
$ReleaseTree = '<release tree>'
git clone --branch $ReleaseTag --depth 1 https://github.com/Habib1001-m/factlane.git
Set-Location factlane
if ((git rev-parse HEAD).Trim() -ne $ReleaseCommit) { throw 'release commit mismatch' }
if ((git rev-parse 'HEAD^{tree}').Trim() -ne $ReleaseTree) { throw 'release tree mismatch' }
uv sync --frozen
uv run python -c "import sqlite3; print(sqlite3.sqlite_version)"
uv run factlane --help-tools
```

وبالنسبة إلى wheel منزلة، قارن SHA-256 الدقيق قبل إنشاء بيئة جديدة، واستدع ملفات Windows التنفيذية من تلك البيئة صراحة:

```powershell
$WheelName = '<wheel filename>'
$WheelSha256 = '<wheel SHA-256>'
if ((Get-FileHash -Algorithm SHA256 $WheelName).Hash.ToLowerInvariant() -ne $WheelSha256.ToLowerInvariant()) { throw 'wheel SHA-256 mismatch' }
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install ".\$WheelName"
.\.venv\Scripts\python.exe -m pip check
.\.venv\Scripts\python.exe -c "import sqlite3; print(sqlite3.sqlite_version)"
.\.venv\Scripts\factlane.exe --help
.\.venv\Scripts\factlane.exe --help-tools
```

تتحقق هذه الأوامر من هوية الحزمة/المصدر وتوافق runtime؛ لكنها لا تخوّل ترحيل قاعدة البيانات ولا تثبت أن خدمة مُدارة أو مضيف MCP أُعيد ربطه بالفعل بهذه bytes.

تُشحن Skill المحمولة `using-factlane` مع الحزمة، لكن تثبيت الحزمة لا يسجل Skill
لدى مضيف MCP تلقائيًا.

## إجراء الترقية للإصدارات اللاحقة

عامل كل انتقال بين إصدارين باعتباره واحدًا من ثلاث فئات تغيير مختلفة قبل تنفيذ أي عمل:

1. **Package/runtime only** — تتغير حزمة Python أو الاعتمادات أو الوثائق أو إعداد
   تشغيل المضيف، بينما يظل تنسيق قاعدة البيانات الحالي متوافقًا بصورة صريحة.
2. **Data/schema migration** — يغير الإصدار الجديد schema المخزنة أو دلالات البيانات
   ويوفر إجراء ترحيل صريحًا.
3. **Compatibility not established** — لا ينص الإصدار على إمكانية استخدام قاعدة بيانات
   موجودة مع وقت التشغيل الجديد. لا تستنتج مسار ترحيل.

### قائمة التحقق قبل الترقية

قبل تبديل نشر قيد التشغيل:

1. سجّل الإصدار الحالي، وهوية المصدر/artifact الدقيقة، وإصدار Python، وإصدار SQLite
   المرتبط، وembedding profile، وإعداد تشغيل المضيف.
2. اقرأ ملاحظات الإصدار المستهدف ودليل التشغيل هذا. حدّد هل الانتقال package-only
   أم يتضمن data/schema migration صريحًا.
3. احتفظ بوقت التشغيل الحالي أو source checkout حتى يظل التراجع على مستوى الحزمة
   ممكنًا ميكانيكيًا.
4. أنشئ نسخة احتياطية يملكها المشغّل وتحقق منها لأي قاعدة بيانات تهم حالتها. وتظل
   سياسات الاحتفاظ بالنسخ الاحتياطية وإجراءات الاستعادة من مسؤوليات النشر.
5. حدّد هدف التراجع **قبل** تغيير البيانات. إذا لم يوثق الإصدار هدف تراجع متوافقًا،
   فاعتبر التراجع عن البيانات غير مدعوم.
6. أوقف FactLane أو أدخله في حالة quiesce قبل أي إجراء خاص بالإصدار ينص على أن
   الوصول الحصري إلى قاعدة البيانات مطلوب.

### ثبّت الهدف بجوار وقت التشغيل الحالي

فضّل بيئة افتراضية جديدة أو source checkout منفصلًا ذا إصدار بدل تعديل البيئة النشطة
في مكانها. تحقق أولًا من ملخص artifact الهدف أو tag/commit/tree، ثم ثبّت باستخدام
مسار الاعتماد المعلن لذلك الإصدار. ولإعادة تشغيل lockfile بدقة، استخدم source checkout
ذا الإصدار وملف `uv.lock` المحفوظ معه.

**لا** تربط وقت التشغيل الجديد بقاعدة بيانات إنتاج موجودة لمجرد نجاح تثبيت الحزمة.
استخدم قاعدة بيانات موجودة فقط عندما ينص الإصدار المستهدف صراحة على أن إصدار المصدر/
تنسيق قاعدة البيانات متوافق، أو يوفر مسار ترحيل مؤهلًا.

## إعادة التحقق بعد التثبيت أو الترقية

قبل تمكين الكتابات، تحقق من الإصدار كما هو مثبت:

1. أكّد إصدار الحزمة والاعتمادات:

   ```bash
   python -c 'from importlib.metadata import version; print(version("factlane"))'
   python -m pip check
   ```

2. أكّد وقت تشغيل SQLite المرتبط من المفسر نفسه:

   ```bash
   python -c 'import sqlite3; print(sqlite3.sqlite_version)'
   ```

3. افحص مرجع الأدوات العامة دون اتصال:

   ```bash
   factlane --help-tools
   ```

   قارن Public Contract Revision المبلغ عنه مع manifest الإصدار المختار. ويبقى سطح MCP العام
   المدعوم بالضبط `memory_search` و`memory_get` و`memory_store` و`memory_update` و`memory_status`؛
   وظهور الأداة لا يمنح صلاحية كتابة.

4. أكّد bytes الخاصة بـ Skill المحمولة لسطح التثبيت. بالنسبة إلى tagged source checkout،
   تحقق من النسخة المصدرية المتتبعة:

   ```bash
   sha256sum skills/using-factlane/SKILL.md
   ```

   وبالنسبة إلى تثبيت wheel أو source distribution داخل `.venv` المستخدمة في الأمثلة أعلاه،
   تحقق من ملف البيانات المثبت:

   ```bash
   sha256sum .venv/share/factlane/skills/using-factlane/SKILL.md
   ```

   قارن bytes المنطبقة مع digests الخاصة بـSkill/reference-set والمسجلة للإصدار المختار.
   ويحافظ source checkout المتزامن باستخدام `uv sync --frozen` على Skill الموثوقة في شجرة
   المصدر ولا يحتاج إلى نسخ ملف البيانات ذلك تحت بادئة البيئة الافتراضية. ويجب أن تتعامل
   الإصدارات التي تشحن مراجع Skill مع Skill على أنها reference set لا ملفًا واحدًا: سجّل وتحقق
   من المسار النسبي وSHA-256 لـ`SKILL.md` ولكل ملف ضمن `references/*`، واطلب تكافؤ bytes بين
   المصدر وwheel وsdist للمجموعة كاملة. وجود الملفات لا يثبت التسجيل في المضيف أو loaded state.
5. شغّل مضيف stdio المهيأ في write profile المقصود وتأكد من أن اكتشاف الأدوات يطابق
   عقد الإصدار. ظهور الأداة لا يمنح سلطة كتابة.
6. عند التحقق من مسار نشر جديد، فضّل قاعدة بيانات disposable لاختبارات smoke. وإذا كان
   الإصدار يتضمن data/schema migration، فنفّذ فحوص السلامة والترحيل الخاصة بالإصدار
   قبل استئناف الخدمة العادية.

## إجراء التراجع

للتراجع معنيان مختلفان ويجب عدم الخلط بينهما.

### التراجع عن package/runtime

إذا لم يحدث تغيير غير متوافق في data/schema، فأوقف وقت التشغيل الجديد وعد إلى البيئة
السابقة المحفوظة أو source checkout ذي الإصدار. أعد التحقق من هوية الحزمة وSQLite
المرتبط والعقد العام وإعداد المضيف قبل استئناف الخدمة.

بالنسبة إلى `v0.1.3`، لا يوجد إصدار إنتاج رسمي أقدم لاستخدامه هدف تراجع رسميًا.
وقدرة مدير الحزم على استبدال إصدارات الحزمة المثبتة لا تنشئ مثل هذا الهدف ولا تثبت
أن خفض إصدار قاعدة البيانات آمن.

### التراجع عن data/schema

لا تشغّل مطلقًا binary أقدم من FactLane على قاعدة بيانات ربما رُحلت أو أصبحت غير متوافقة
بسبب إصدار أحدث، ما لم يعلن الإصدار الأحدث صراحة أن downgrade آمن. يتطلب التراجع عن
data/schema ترحيلًا عكسيًا خاصًا بالإصدار أو استعادة نسخة احتياطية متحقق منها من قبل
الترقية، تحت وقت تشغيل موثق بأنه متوافق مع تلك النسخة الاحتياطية.

بالنسبة إلى `v0.1.3`، لا يُدّعى وجود عقد لترحيل بيانات الإنتاج عبر الإصدارات أو خفض
الإصدار. إذا احتاج نشر ما إلى هذا المسار، فتحقق منه لذلك النشر قبل الاعتماد عليه.

## قائمة تحقق مؤلف الإصدار للإصدارات المستقبلية

ينبغي أن يستخدم كل إصدار رسمي لاحق قائمة التحقق هذه:

1. قبل النشر، اذكر ملف Python/SQLite/runtime المطلوب وpublic contract revision/tool set
   الدقيقين.
2. صنّف الانتقال من كل إصدار رسمي سابق مدعوم على أنه package-only أو data/schema migration
   أو unsupported.
3. إذا وجدت تغييرات data/schema، فوثّق متطلبات النسخ الاحتياطي، والترحيل إلى الأمام،
   وفحوص السلامة بعد الترحيل، ومسار التراجع أو الاستعادة المدعوم قبل الإصدار.
4. جمّد release tree دقيقة واحدة؛ وابنِ wheel وsource distribution من تلك الشجرة؛
   وتحقق من التثبيت النظيف وpackage metadata وتكافؤ runtime-source وتكافؤ reference set
   الكامل لـSkill المحمولة (المسارات النسبية + SHA-256، وليس `SKILL.md` وحده).
   استخدم `scripts/build_qualified_package.py` لبناءات التأهيل: فهو يصدّر الشجرة الملتزم بها بدقة
   إلى مجلد مؤقت قبل تشغيل `uv build`، بحيث لا يمكن لحالة `build/` أو `*.egg-info/` القديمة أو
   المتجاهلة في checkout مُعاد الاستخدام أن تدخل حمولة الإصدار. لا تستخدم `uv build --wheel`
   مباشرةً من worktree مُعاد الاستخدام كدليل إصدار.
5. أعد التحقق من مضيف MCP المهيأ ومجموعة الأدوات العامة الدقيقة قبل النشر.
6. انشر الوسم والأصول فقط بعد اجتياز بوابات الإصدار. لا تغيّر وسمًا منشورًا بالفعل
   ولا تستبدل أصوله لجعل وثائق لاحقة تتفق معه.
7. سجّل tag object وcommit وtree وأسماء artifacts وملخصات SHA-256 النهائية في manifest
   ذي الإصدار. وإذا لم يمكن معرفة هذه الهويات حتى النشر، فسجّلها في commit لاحق على
   `main` مخصص للوثائق فقط بدل تعديل released tag/tree.
8. رحّل إلى الأمام ادعاءات الدعم المدعومة بالأدلة فقط. لا يجوز توسيع ادعاءات الجودة
   الدلالية للعربية/المحتوى مختلط اللغات، أو kernel `ENOSPC`، أو `EROFS` الحقيقي،
   أو سلوك نظام الملفات، أو تغطية العملاء، أو حدود الحجم من دون دليل مباشر.
9. لا تُعد توجيه وسم إصدار سابق ولا تستبدل أصوله المنشورة. استمر في التحقق من الإصدارات
   التاريخية عبر commit/tree المسجلين وartifact digests. ولا يعيد تغيير صيانة للوثائق
   على `main` كتابة إصدار تاريخي.
10. إذا قبل إصدار فضاء تضمين قائمًا عبر انتقال runtime/provider، فسجّل رقم `revision` وهوية التوافق
    الدلالي بصورة منفصلة عن provenance وقت التشغيل، وجمّد حزمة cross-space anchors المؤهلة كمادة
    إصدار مربوطة بالسلامة، واطلب تكافؤ bytes لتلك الحزمة بين المصدر وwheel وsource distribution.
11. لكل انتقال تضمين legacy مدعوم، أثبت هل الترحيل metadata-only أم يتطلب re-embedding صريحًا.
    ويجب أن يثبت انتقال metadata-only عدم إعادة كتابة أي vector والحفاظ على provenance التضمين
    لكل سجل. أما الانتقال غير المتوافق أو غير القابل للإثبات فيجب أن يحفظ الحقائق الدائمة ويفشل
    عمليات semantic/تغيير المتجهات في وضع مغلق إلى أن يُخوّل re-embedding صريح أو مسار restore متحقق منه.

راجع [البدء السريع](QUICKSTART.md) لإعداد المضيف لأول مرة، و[البيئة](ENVIRONMENT.md)
لمتطلبات وقت التشغيل، و[المعمارية](ARCHITECTURE.md) لحدود التخزين والعقد،
و[الأمان](SECURITY.md) لقيود الاستعادة والثقة.
