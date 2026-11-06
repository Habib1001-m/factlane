import { currentLocale } from "./shell.js";

const KEYS = "overviewTitle,overviewLead,logicalMemories,recordedRevisions,awaitingReview,conflictGroups,coverage,recentActivity,needsAttention,reviewCandidates,browseTitle,browseLead,search,searchMode,textSearch,semanticSearch,scope,projectId,worktreeId,workflowId,agentId,sourceRef,state,addCandidate,createCandidate,fact,memoryType,sourceClass,sourceHash,reviewRef,extractionMethod,sourceTimestamp,verifiedBy,authority,freshness,lastVerified,subject,noResults,unavailable,retry,noCandidates,tableRegion,listLead,detailTitle,detailLead,history,lifecycleEvents,lifecycleActions,revoke,reverify,replace,manualRecheck,readmit,permanentPurge,submitAction,replacementFact,claim,recheckRef,observedAt,observedSourceHash,evidenceRef,method,reviewTitle,reviewLead,proposedFact,verification,approve,reject,reason,conflictsTitle,conflictsLead,groupMembers,affectedRecords,resolutionReason,selectedWinner,resolveConflict,databaseTitle,databaseLead,databaseIdentity,backups,importedNeedReview,createBackup,exportJson,importPreview,importCommit,restorePreview,housekeepingPreview,runHousekeeping,maxRecords,requiredFreeBytes,deleteBackup,download,trustedOrigin,restoreAck,restoreLead,restoreSummary,authorize,recoveryTitle,recoveryLead,recoverySteps,resume,purgeTitle,purgePreview,previewDigest,expectedConfirmation,typedConfirmation,requestSensitiveApproval,checkNativeApproval,commitPurge,retryPurge,nativeApprovalPhrase,agentsTitle,agentsLead,runtimeObservations,integrationGuidance,reviewConnectionSettings,activityTitle,activityLead,recordedEvents,diagnostics,previewDiagnostics,downloadDiagnostics,settingsTitle,settingsLead,editableSettings,previewChanges,applyChanges,service,enabledAtBoot,stopUntilReboot,restartWebui,openLauncher,sessionAndDevices,appearance,inactivity,maximumAge,language,theme,endSession,manageDevice,connectTitle,connectLead,approval,waiting,requestLocator,claimRequest,launcherApproval,cancel,identifier,content,source,project,candidates,memories,lastObserved,action,actor,result,time,correlation,runtime,configured,observed,observationSource,pendingReload,artifact,created,size,compatibility,integrity,health,storage,device,none".split(",");
const WORDS = [["Overview","Understand the selected database and what needs review.","Logical memories","Recorded revisions","Awaiting review","Conflict groups","Project and scope coverage","Recent activity","Needs attention","Review candidates","Browse memory","Search and narrow the Owner view.","Search","Search mode","Text","Semantic","Scope","Project ID","Worktree ID","Workflow ID","Agent ID","Source reference","State","Add candidate","Create Candidate","Fact","Memory type","Source class","Source hash","Review reference","Extraction method","Source timestamp","Verified by","Authority","Freshness","Last verified","Subject","No records to display.","Unavailable","Retry","No Candidates","Scrollable data table","Review records in the selected scope.","Memory detail","Inspect content, scope, state, and provenance together.","Revision history","Lifecycle events","Lifecycle actions","Revoke","Reverify","Replace","Manual recheck","Readmit","Permanent purge","Submit action","Replacement fact","Claim","Recheck reference","Observed at","Observed source hash","Evidence reference","Method","Review a Candidate","Verify the proposed fact against its evidence before choosing an action.","Proposed fact","Verification evidence","Approve with verification","Reject with reason","Reason","Conflicts","Compare group members and source evidence before resolving a conflict.","Group members","Affected records preview","Resolution reason","Selected winner","Confirm resolution","Database and backups","Review database identity, health, storage, and explicit backup operations.","Database identity","Backups","Imported facts remain Candidate/unverified until review.","Create backup","Export JSON","Import preview","Import as Candidates","Restore preview","Preview housekeeping","Run housekeeping","Maximum records","Required free bytes","Delete artifact","Download","Trusted origin attested","I acknowledge active database state will be replaced","Review the selected restore inputs before authorization.","Replacement summary","Authorize","Recovery requires attention","Use the recorded recovery state to decide the next bounded action.","Recovery steps","Authorize and resume","Permanent purge authorization","Exact purge preview","Preview digest","Required typed confirmation","Type the confirmation exactly","Request sensitive approval","Check native approval","Commit permanent purge","Retry recovery","Native approval phrase","Agents and connections","Compare configured permissions with direct runtime observations.","Runtime observations","Integration guidance","Review connection settings","Activity","Trace actions, jobs, scopes, results, and correlation identifiers.","Recorded events","Diagnostics","Preview diagnostics","Download diagnostics","Settings and service","Review service state, launcher controls, sessions, devices, and appearance.","Editable settings","Preview changes","Apply changes","WebUI service","Enabled at boot","Stop until next reboot","Restart through launcher","Open FactLane launcher","Session and devices","Language and theme","Inactivity","Maximum age","Language","Theme","End this session","Manage device","Connect this browser","Review the approval request presented by the trusted flow.","Approval request","Waiting for approval details.","Request locator","Claim request","Open the FactLane launcher to approve this browser.","Cancel request","Identifier","Content","Source","Project / scope","Candidates","Memories","Last observed","Action","Actor","Result","Time","Correlation","Runtime","Configured permission","Observed permission","Observation source","Pending reload","Artifact","Created","Size","Compatibility","Integrity check","Health","Storage","Device","Not provided"],["نظرة عامة","افهم قاعدة البيانات المحددة وما يحتاج إلى مراجعة.","الذكريات المنطقية","المراجعات المسجلة","بانتظار المراجعة","مجموعات التعارض","تغطية المشاريع والنطاقات","النشاط الأخير","يحتاج إلى انتباه","مراجعة المرشحين","تصفح الذاكرة","ابحث وضيّق نطاق عرض المالك.","البحث","وضع البحث","نصي","دلالي","النطاق","معرّف المشروع","معرّف شجرة العمل","معرّف سير العمل","معرّف الوكيل","مرجع المصدر","الحالة","إضافة مرشّح","إنشاء مرشّح","الحقيقة","نوع الذاكرة","فئة المصدر","بصمة المصدر","مرجع المراجعة","طريقة الاستخراج","وقت المصدر","تم التحقق بواسطة","السلطة","الحداثة","آخر تحقق","الموضوع","لا توجد سجلات للعرض.","غير متاح","إعادة المحاولة","لا توجد مرشحات","جدول بيانات قابل للتمرير","راجع السجلات في النطاق المحدد.","تفاصيل الذاكرة","افحص المحتوى والنطاق والحالة والمصدر معًا.","سجل المراجعات","أحداث دورة الحياة","إجراءات دورة الحياة","سحب","إعادة تحقق","استبدال","إعادة فحص يدوي","إعادة قبول","حذف دائم","إرسال الإجراء","الحقيقة البديلة","الادعاء","مرجع إعادة الفحص","وقت الرصد","بصمة المصدر المرصودة","مرجع الدليل","الطريقة","مراجعة مرشّح","تحقق من الحقيقة المقترحة مقابل دليلها قبل اختيار الإجراء.","الحقيقة المقترحة","دليل التحقق","اعتماد بعد التحقق","رفض مع السبب","السبب","التعارضات","قارن أعضاء المجموعة وأدلة المصدر قبل حل التعارض.","أعضاء المجموعة","معاينة السجلات المتأثرة","سبب الحل","الفائز المحدد","تأكيد الحل","قاعدة البيانات والنسخ الاحتياطية","راجع هوية قاعدة البيانات وصحتها والتخزين وعمليات النسخ الاحتياطي الصريحة.","هوية قاعدة البيانات","النسخ الاحتياطية","تبقى الحقائق المستوردة مرشحة وغير متحقق منها حتى المراجعة.","إنشاء نسخة احتياطية","تصدير JSON","معاينة الاستيراد","استيراد كمرشحات","معاينة الاستعادة","معاينة الصيانة","تشغيل الصيانة","الحد الأقصى للسجلات","المساحة الحرة المطلوبة بالبايت","حذف الملف","تنزيل","تم التحقق من موثوقية المصدر","أقر بأن حالة قاعدة البيانات النشطة ستُستبدل","راجع مدخلات الاستعادة المحددة قبل التفويض.","ملخص الاستبدال","تفويض","التعافي يحتاج إلى انتباه","استخدم حالة التعافي المسجلة لتحديد الإجراء المحدود التالي.","خطوات التعافي","تفويض ومتابعة","تفويض الحذف الدائم","معاينة الحذف الدقيقة","بصمة المعاينة","نص التأكيد المطلوب","اكتب التأكيد حرفيًا","طلب موافقة حساسة","فحص الموافقة المحلية","تنفيذ الحذف الدائم","إعادة محاولة التعافي","عبارة الموافقة المحلية","الوكلاء والاتصالات","قارن الصلاحيات المضبوطة بالملاحظات المباشرة لوقت التشغيل.","ملاحظات وقت التشغيل","إرشادات التكامل","مراجعة إعدادات الاتصال","النشاط","تتبّع الإجراءات والمهام والنطاقات والنتائج ومعرّفات الارتباط.","الأحداث المسجلة","التشخيصات","معاينة التشخيصات","تنزيل التشخيصات","الإعدادات والخدمة","راجع حالة الخدمة وعناصر تحكم المشغّل والجلسات والأجهزة والمظهر.","الإعدادات القابلة للتعديل","معاينة التغييرات","تطبيق التغييرات","خدمة WebUI","مفعّلة عند الإقلاع","إيقاف حتى إعادة التشغيل التالية","إعادة التشغيل عبر المشغّل","فتح مشغّل FactLane","الجلسة والأجهزة","اللغة والمظهر","الخمول","الحد الأقصى للعمر","اللغة","المظهر","إنهاء هذه الجلسة","إدارة الجهاز","اتصال هذا المتصفح","راجع طلب الموافقة الذي يقدمه المسار الموثوق.","طلب الموافقة","في انتظار تفاصيل الموافقة.","معرّف الطلب","المطالبة بالطلب","افتح مشغّل FactLane للموافقة على هذا المتصفح.","إلغاء الطلب","المعرّف","المحتوى","المصدر","المشروع / النطاق","المرشحون","الذكريات","آخر ملاحظة","الإجراء","الفاعل","النتيجة","الوقت","الارتباط","وقت التشغيل","الصلاحية المضبوطة","الصلاحية المرصودة","مصدر الملاحظة","إعادة تحميل معلقة","الملف","الإنشاء","الحجم","التوافق","فحص السلامة","الصحة","التخزين","الجهاز","غير متاح"]];
const COPIES = WORDS.map((words) => Object.fromEntries(KEYS.map((key, index) => [key, words[index]])));

const SCOPE_OPTIONS = Object.freeze(["PROJECT", "GLOBAL_USER", "WORKFLOW", "TOOL_ENVIRONMENT", "CROSS_PROJECT_WORKFLOW"].map((value) => Object.freeze([value, value])));

function copy() {
  return COPIES[currentLocale() === "ar" ? 1 : 0];
}

function element(tag, { className = "", text = "", attrs = {} } = {}, children = []) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== "") node.textContent = String(text);
  for (const [name, value] of Object.entries(attrs)) {
    if (value !== undefined && value !== null) node.setAttribute(name, String(value));
  }
  for (const child of children) if (child) node.append(child);
  return node;
}

const box = (children) => element("section", { className: "panel" }, children);
const fieldGrid = (children) => element("div", { className: "field-grid" }, children);
const titled = (title, children) => box([element("h2", { text: title }), ...children]);

function page(title, lead, children = []) {
  const head = element("div", { className: "page-head" }, [
    element("div", {}, [element("h1", { text: title }), element("p", { text: lead })]),
  ]);
  return element("div", { attrs: { "data-view": "" } }, [head, ...children]);
}

function valueOrFallback(value) {
  return value === undefined || value === null || value === "" ? copy().none : value;
}

function identifier(value) {
  return element("bdi", { className: "mono", text: valueOrFallback(value), attrs: { dir: "auto" } });
}

function metricValue(value) {
  if (value && typeof value === "object" && !Array.isArray(value)) {
    if (value.value !== undefined && value.value !== null) return value.value;
    if (value.completeness && value.completeness !== "EXACT") return copy().unavailable;
  }
  return valueOrFallback(value);
}

function metaList(entries) {
  const list = element("dl", { className: "meta" });
  for (const [label, value] of entries) {
    const description = element("dd");
    description.append(asNode(value));
    list.append(element("dt", { text: label }), description);
  }
  return list;
}

function actionButton(label, { primary = false, danger = false, onClick } = {}) {
  const className = ["action-button", primary ? "primary" : "", danger ? "danger" : ""].filter(Boolean).join(" ");
  const button = element("button", { className, text: label, attrs: { type: "button" } });
  button.disabled = typeof onClick !== "function";
  if (typeof onClick === "function") button.addEventListener("click", onClick);
  return button;
}

function emptyState(message) {
  return element("div", { className: "empty-state", text: message });
}

function asNode(value) {
  return value instanceof Node ? value : document.createTextNode(String(valueOrFallback(value)));
}

function dataTable(headers, rows, label = copy().tableRegion) {
  if (!rows.length) return emptyState(copy().noResults);
  const headRow = element("tr");
  for (const header of headers) headRow.append(element("th", { text: header, attrs: { scope: "col" } }));
  const body = element("tbody");
  for (const rowValues of rows) {
    const row = element("tr");
    for (const value of rowValues) {
      const cell = element("td");
      cell.append(asNode(value));
      row.append(cell);
    }
    body.append(row);
  }
  const table = element("table", { className: "data-table" }, [
    element("thead", {}, [headRow]),
    body,
  ]);
  return element("div", { className: "table-wrap", attrs: { role: "region", "aria-label": label, tabindex: "0" } }, [table]);
}

function metric(label, value, onRetry) {
  const unavailable = value && typeof value === "object" && value.value == null && value.completeness !== "EXACT";
  const children = [
    element("span", { text: label }),
    element("strong", { text: metricValue(value) }),
  ];
  if (unavailable && value.reason) children.push(element("span", { className: "small muted", text: value.reason }));
  if (unavailable && typeof onRetry === "function") children.push(actionButton(copy().retry, { onClick: onRetry }));
  return element("div", { className: "metric" }, children);
}

function actionGroup(buttons) {
  return element("div", { className: "page-actions" }, buttons.filter(Boolean));
}

const singleAction = (label, options) => actionGroup([actionButton(label, options)]);

function callbackFor(callback, value) {
  return typeof callback === "function" ? () => callback(value) : undefined;
}

function labeledInput(label, name, value = "", { type = "text", readOnly = false } = {}) {
  const input = element("input", { attrs: { type, name, value: value ?? "", autocomplete: "off" } });
  input.readOnly = readOnly;
  return element("label", { text: label }, [input]);
}

function fileInput(label, name) {
  return element("label", { text: label }, [element("input", { attrs: { type: "file", name } })]);
}

function checkInput(label, name, checked = false) {
  const input = element("input", { className: "inline-check", attrs: { type: "checkbox", name } });
  input.checked = checked;
  return element("label", {}, [input, document.createTextNode(label)]);
}

function formPanel(title, fields, action, onSubmit) {
  const form = element("form", { className: "panel" }, [element("h2", { text: title }), ...fields]);
  form.append(singleAction(action, { primary: true, onClick: typeof onSubmit === "function" ? () => form.requestSubmit() : undefined }));
  form.addEventListener("submit", (event) => { event.preventDefault(); onSubmit?.(formValues(form)); });
  return form;
}

function selectInput(label, name, value, options) {
  const select = element("select", { attrs: { name } });
  for (const [optionValue, optionLabel] of options) {
    const option = element("option", { text: optionLabel, attrs: { value: optionValue } });
    if (optionValue === value) option.selected = true;
    select.append(option);
  }
  return element("label", { text: label }, [select]);
}

function formValues(form) {
  return Object.fromEntries(new FormData(form).entries());
}

function scopeInputs(c, value = {}) {
  return [
    selectInput(c.scope, "scope", value.scope || "PROJECT", SCOPE_OPTIONS),
    labeledInput(c.projectId, "projectId", value.projectId || ""),
    labeledInput(c.worktreeId, "worktreeId", value.worktreeId || ""),
    labeledInput(c.workflowId, "workflowId", value.workflowId || ""),
    labeledInput(c.agentId, "agentId", value.agentId || ""),
  ];
}

function sourceInputs(c, value = {}) {
  return [
    labeledInput(c.sourceClass, "sourceClass", value.sourceClass || "OWNER"),
    labeledInput(c.sourceRef, "sourceRef", value.sourceRef || ""),
    labeledInput(c.sourceHash, "sourceHash", value.sourceHash || ""),
    labeledInput(c.reviewRef, "reviewRef", value.reviewRef || ""),
    labeledInput(c.extractionMethod, "extractionMethod", value.extractionMethod || "manual"),
    labeledInput(c.sourceTimestamp, "sourceTimestamp", value.sourceTimestamp || ""),
  ];
}

function memoryCard(item, onOpenDetail) {
  const c = copy();
  const card = element("article", { className: "memory-card" });
  const heading = element("div", { className: "row" }, [
    element("strong", {}, [identifier(item.memory_id || item.id)]),
    item.state ? element("span", { className: "badge", text: item.state }) : null,
  ]);
  card.append(heading);
  if (item.fact || item.content) card.append(element("p", { text: item.fact || item.content }));
  card.append(metaList([[c.scope, item.scope], [c.source, item.source || item.source_provenance?.source_ref]]));
  if (typeof onOpenDetail === "function") card.append(singleAction(c.detailTitle, { onClick: callbackFor(onOpenDetail, item) }));
  return card;
}

export function renderOverviewState({ totals = {}, coverage = [], activity = [], attention = [], activityUnavailable = false, attentionUnavailable = false, onReviewCandidates, onReviewAttention, onRetryOverview } = {}) {
  const c = copy();
  const metrics = element("section", { className: "metrics", attrs: { "aria-label": c.overviewTitle } }, [
    metric(c.logicalMemories, totals.logicalMemories, onRetryOverview),
    metric(c.recordedRevisions, totals.revisions, onRetryOverview),
    metric(c.awaitingReview, totals.candidates, onRetryOverview),
    metric(c.conflictGroups, totals.conflicts, onRetryOverview),
  ]);
  const coverageRows = coverage.map((item) => [
    item.project || item.scope,
    metricValue(item.memories),
    metricValue(item.candidates) === 0 ? c.noCandidates : metricValue(item.candidates),
    item.lastObserved,
  ]);
  const coveragePanel = titled(c.coverage, [
    dataTable([c.project, c.memories, c.candidates, c.lastObserved], coverageRows),
  ]);
  const activityPanel = titled(c.recentActivity, [
    activityUnavailable
      ? emptyState(c.unavailable)
      : activity.length
      ? element("div", { className: "history-list" }, activity.map((item) =>
        element("article", { className: "history-item" }, [
          element("strong", { text: valueOrFallback(item.action || item.title) }),
          item.detail ? element("p", { className: "muted small", text: item.detail }) : null,
          item.time ? element("p", { className: "muted small", text: item.time }) : null,
        ])))
      : emptyState(c.noResults),
  ]);
  const attentionPanel = titled(c.needsAttention, [
    attentionUnavailable
      ? emptyState(c.unavailable)
      : attention.length
      ? element("div", { className: "cards" }, attention.map((item) =>
        element("article", { className: "memory-card" }, [
          element("strong", { text: valueOrFallback(item.title || item.state) }),
          item.detail ? element("p", { text: item.detail }) : null,
          singleAction(c.reviewCandidates, { onClick: callbackFor(onReviewAttention, item) }),
        ])))
      : emptyState(c.noResults),
  ]);
  const headAction = singleAction(c.reviewCandidates, { primary: true, onClick: onReviewCandidates });
  return page(c.overviewTitle, c.overviewLead, [headAction, metrics, element("div", { className: "columns" }, [coveragePanel, activityPanel]), attentionPanel]);
}

export function renderBrowseState({
  filters: suppliedFilters = {},
  items = [],
  searchSummary = "",
  candidateDraft = null,
  onSearch,
  onAddCandidate,
  onCandidateDraftChange,
  onCreateCandidate,
  onOpenDetail,
} = {}) {
  const c = copy();
  const f = {
    query: "",
    searchMode: "TEXT",
    scope: "PROJECT",
    projectId: "",
    worktreeId: "",
    workflowId: "",
    agentId: "",
    lifecycleState: "",
    sourceRef: "",
    ...suppliedFilters,
  };
  const searchForm = element("form", { className: "panel" });
  const searchFields = fieldGrid([
    labeledInput(c.search, "query", f.query, { type: "search" }),
    selectInput(c.searchMode, "searchMode", f.searchMode, [["TEXT", c.textSearch], ["SEMANTIC", c.semanticSearch]]),
    ...scopeInputs(c, f),
    labeledInput(c.state, "lifecycleState", f.lifecycleState),
    labeledInput(c.sourceRef, "sourceRef", f.sourceRef),
  ]);
  searchForm.append(searchFields, singleAction(c.search, { primary: true, onClick: typeof onSearch === "function" ? () => searchForm.requestSubmit() : undefined }));
  if (typeof onSearch === "function") {
    searchForm.addEventListener("submit", (event) => {
      event.preventDefault();
      onSearch(formValues(searchForm));
    });
  }
  const filters = searchForm;
  const results = element("section", { className: "cards", attrs: { "aria-live": "polite", "data-results": "" } },
    items.length ? items.map((item) => memoryCard(item, onOpenDetail)) : [emptyState(c.noResults)]);
  const children = [
    singleAction(c.addCandidate, { primary: true, onClick: onAddCandidate }),
    filters,
    searchSummary ? element("p", { className: "notice", text: searchSummary, attrs: { role: "status" } }) : null,
    results,
  ];
  if (candidateDraft) {
    const draftForm = element("form", { className: "panel candidate-form" });
    const fields = fieldGrid([
      labeledInput(c.fact, "fact", candidateDraft.fact || ""),
      labeledInput(c.memoryType, "memoryType", candidateDraft.memoryType || "PROJECT_LEARNED_FACT"),
      ...scopeInputs(c, candidateDraft),
      ...sourceInputs(c, candidateDraft),
      labeledInput(c.subject, "subject", candidateDraft.subject || ""),
    ]);
    draftForm.append(element("h2", { text: c.createCandidate }), fields, singleAction(c.createCandidate, { primary: true, onClick: typeof onCreateCandidate === "function" ? () => draftForm.requestSubmit() : undefined }));
    draftForm.addEventListener("input", () => onCandidateDraftChange?.(formValues(draftForm)));
    draftForm.addEventListener("submit", (event) => {
      event.preventDefault();
      onCandidateDraftChange?.(formValues(draftForm));
      onCreateCandidate?.(formValues(draftForm));
    });
    children.splice(2, 0, draftForm);
  }
  return page(c.browseTitle, c.browseLead, children.filter(Boolean));
}

export function renderListState({ items = [] } = {}) {
  const c = copy();
  const contents = items.length ? items.map(memoryCard) : [emptyState(c.noResults)];
  return page(c.memories, c.listLead, [element("section", { className: "cards" }, contents)]);
}

function renderDetailLifecyclePanel({ memory = {}, draft = null, onOpenAction, onDraftChange, onSubmit, onPurgePreview } = {}) {
  const c = copy();
  const lifecycle = memory.lifecycle_state || memory.state;
  const actions = [];
  if (!new Set(["REVOKED", "REJECTED"]).has(lifecycle)) {
    actions.push(
      actionButton(c.revoke, { danger: true, onClick: callbackFor(onOpenAction, "REVOKE") }),
      actionButton(c.reverify, { onClick: callbackFor(onOpenAction, "REVERIFY") }),
      actionButton(c.replace, { onClick: callbackFor(onOpenAction, "REPLACE") }),
      actionButton(c.manualRecheck, { onClick: callbackFor(onOpenAction, "MANUAL_RECHECK") }),
    );
  }
  if (lifecycle === "REVOKED") actions.push(actionButton(c.readmit, { primary: true, onClick: callbackFor(onOpenAction, "READMIT") }));
  actions.push(actionButton(c.permanentPurge, { danger: true, onClick: onPurgePreview }));

  const panel = titled(c.lifecycleActions, [
    actionGroup(actions),
  ]);
  if (!draft?.action) return panel;

  const form = element("form", { className: "lifecycle-form stack" });
  const fields = element("div", { className: "field-grid" });
  if (draft.action === "REVOKE") {
    fields.append(selectInput(c.reason, "reason", draft.reason || "OUTDATED", [
      ["INCORRECT", "INCORRECT"],
      ["OUTDATED", "OUTDATED"],
      ["WITHDRAWN", "WITHDRAWN"],
      ["POLICY_REVOKED", "POLICY_REVOKED"],
    ]));
  } else if (draft.action === "MANUAL_RECHECK") {
    fields.append(
      selectInput(c.claim, "claim", draft.claim || "INDETERMINATE", [
        ["OBSERVED_HASH", "OBSERVED_HASH"],
        ["UNAVAILABLE", "UNAVAILABLE"],
        ["INDETERMINATE", "INDETERMINATE"],
      ]),
      labeledInput(c.recheckRef, "recheckRef", draft.recheckRef || ""),
      labeledInput(c.observedAt, "observedAt", draft.observedAt || ""),
      labeledInput(c.observedSourceHash, "observedSourceHash", draft.observedSourceHash || ""),
      labeledInput(c.evidenceRef, "evidenceRef", draft.evidenceRef || ""),
      selectInput(c.method, "method", draft.method || "OWNER_REVIEWED_SOURCE", [
        ["OWNER_HASHED_SOURCE", "OWNER_HASHED_SOURCE"],
        ["OWNER_REVIEWED_SOURCE", "OWNER_REVIEWED_SOURCE"],
        ["TRUSTED_LOCAL_RESULT_REFERENCE", "TRUSTED_LOCAL_RESULT_REFERENCE"],
      ]),
    );
  } else {
    if (draft.action === "REPLACE") fields.append(labeledInput(c.replacementFact, "fact", draft.fact || memory.fact || memory.content || ""));
    fields.append(
      ...sourceInputs(c, draft),
    );
  }
  form.append(
    element("h3", { text: `${c.lifecycleActions}: ${draft.action}` }),
    fields,
    singleAction(c.submitAction, { primary: true, onClick: typeof onSubmit === "function" ? () => form.requestSubmit() : undefined }),
  );
  form.addEventListener("input", () => onDraftChange?.(formValues(form)));
  form.addEventListener("change", () => onDraftChange?.(formValues(form)));
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    const values = formValues(form);
    onDraftChange?.(values);
    onSubmit?.(values);
  });
  panel.append(form);
  return panel;
}

export function renderDetailState({
  memory = {},
  records = [],
  events = [],
  lifecycleDraft = null,
  onReview,
  onOpenLifecycleAction,
  onLifecycleDraftChange,
  onLifecycleAction,
  onPurgePreview,
} = {}) {
  const c = copy();
  const panel = element("section", { className: "panel detail-panel" }, [
    element("h2", { text: c.content }),
    memory.fact || memory.content ? element("p", { text: memory.fact || memory.content }) : emptyState(c.none),
    metaList([
      [c.identifier, identifier(memory.memory_id || memory.id)],
      [c.state, memory.lifecycle_state || memory.state],
      [c.scope, memory.scope],
      [c.source, memory.source_provenance?.source_ref || memory.source],
      [c.sourceClass, memory.source_provenance?.source_class],
      [c.sourceHash, memory.source_provenance?.source_hash ? identifier(memory.source_provenance.source_hash) : null],
      [c.reviewRef, memory.source_provenance?.review_ref],
      [c.extractionMethod, memory.source_provenance?.extraction_method],
      [c.sourceTimestamp, memory.source_timestamp],
      [c.verifiedBy, memory.verified_by],
      [c.authority, memory.authority_role],
      [c.freshness, memory.freshness_gate_state || memory.freshness_policy?.kind],
      [c.lastVerified, memory.last_verified_at],
    ]),
    (memory.lifecycle_state || memory.state) === "CANDIDATE" ? singleAction(c.reviewTitle, { primary: true, onClick: onReview }) : null,
  ]);
  const historyItems = records.length
    ? records.map((record) => element("article", { className: "history-item" }, [
      element("h3", { text: `Revision ${record.revision}` }),
      record.fact ? element("p", { text: record.fact }) : emptyState(c.none),
      metaList([
        [c.identifier, identifier(record.record_id)],
        [c.state, record.lifecycle_state],
        [c.verifiedBy, record.verified_by],
        [c.authority, record.authority_role],
        [c.source, record.source_provenance?.source_ref],
        [c.lastVerified, record.last_verified_at],
      ]),
    ]))
    : [emptyState(c.noResults)];
  const eventRows = events.map((event) => [event.occurred_at, event.action, event.prior_lifecycle_state, event.result_lifecycle_state, identifier(event.event_id)]);
  const history = box([element("h2", { text: c.history }), element("div", { className: "history-list" }, historyItems)]);
  const lifecycle = box([element("h2", { text: c.lifecycleEvents }), dataTable([c.time, c.action, "Before", "After", c.identifier], eventRows, c.lifecycleEvents)]);
  const actions = renderDetailLifecyclePanel({
    memory,
    draft: lifecycleDraft,
    onOpenAction: onOpenLifecycleAction,
    onDraftChange: onLifecycleDraftChange,
    onSubmit: onLifecycleAction,
    onPurgePreview,
  });
  return page(c.detailTitle, c.detailLead, [panel, actions, history, lifecycle]);
}

export function renderReviewState({ candidate = {}, verification = {}, draft = {}, onDraftChange, onApprove, onReject } = {}) {
  const c = copy();
  const source = {
    sourceClass: verification.source_provenance?.source_class,
    sourceRef: verification.source_provenance?.source_ref,
    sourceHash: verification.source_provenance?.source_hash,
    reviewRef: verification.source_provenance?.review_ref,
    extractionMethod: verification.source_provenance?.extraction_method,
    sourceTimestamp: verification.source_timestamp,
    ...draft,
  };
  const proposed = titled(c.proposedFact, [
    candidate.content ? element("p", { text: candidate.content }) : emptyState(c.none),
    metaList([[c.identifier, identifier(candidate.id)], [c.state, candidate.state], [c.scope, candidate.scope]]),
  ]);
  const evidence = titled(c.verification, [
    metaList([[c.source, verification.source], [c.state, verification.status]]),
  ]);
  const form = element("form", { className: "panel" });
  const fields = fieldGrid([
    ...sourceInputs(c, source),
    selectInput(c.reason, "reason", draft.reason || "INSUFFICIENT_EVIDENCE", [
      ["INSUFFICIENT_EVIDENCE", "INSUFFICIENT_EVIDENCE"],
      ["INCORRECT", "INCORRECT"],
      ["DUPLICATE", "DUPLICATE"],
      ["CONTRADICTED", "CONTRADICTED"],
      ["POLICY_REJECTED", "POLICY_REJECTED"],
    ]),
  ]);
  const approve = actionButton(c.approve, { primary: true, onClick: typeof onApprove === "function" ? () => onApprove(formValues(form)) : undefined });
  const reject = actionButton(c.reject, { onClick: typeof onReject === "function" ? () => onReject(formValues(form)) : undefined });
  form.append(fields, actionGroup([approve, reject]));
  form.addEventListener("input", () => onDraftChange?.(formValues(form)));
  return page(c.reviewTitle, c.reviewLead, [element("div", { className: "columns equal" }, [proposed, evidence]), form]);
}

export function renderConflictsState({
  group = {},
  members = [],
  preview = [],
  selectedWinner = "",
  reason = "",
  onWinnerChange,
  onReasonChange,
  onResolve,
} = {}) {
  const c = copy();
  const memberCards = members.length
    ? members.map((member) => element("article", { className: "memory-card" }, [
      element("div", { className: "row" }, [
        element("strong", {}, [identifier(member.record_id || member.id)]),
        member.state ? element("span", { className: "badge", text: member.state }) : null,
      ]),
      member.content ? element("p", { text: member.content }) : null,
      metaList([[c.source, member.source], [c.state, member.verificationStatus]]),
    ]))
    : [emptyState(c.noResults)];
  const membersPanel = titled(c.groupMembers, [
    group.id ? element("p", { className: "muted small mono", text: group.id }) : null,
    element("div", { className: "cards" }, memberCards),
  ]);
  const winnerSelect = element("select", { attrs: { name: "winner" } });
  winnerSelect.append(element("option", { text: c.none, attrs: { value: "" } }));
  for (const member of members) {
    const value = member.id || "";
    const option = element("option", { text: valueOrFallback(value), attrs: { value } });
    if (value && value === selectedWinner) option.selected = true;
    winnerSelect.append(option);
  }
  winnerSelect.disabled = typeof onWinnerChange !== "function";
  if (typeof onWinnerChange === "function") winnerSelect.addEventListener("change", () => onWinnerChange(winnerSelect.value));
  const reasonLabel = selectInput(c.resolutionReason, "reason", reason || "OWNER_ADJUDICATION", [
    ["OWNER_ADJUDICATION", "OWNER_ADJUDICATION"],
    ["STRONGER_EVIDENCE", "STRONGER_EVIDENCE"],
    ["SOURCE_PRECEDENCE", "SOURCE_PRECEDENCE"],
    ["NEWER_VERIFIED_TRUTH", "NEWER_VERIFIED_TRUTH"],
  ]);
  const reasonInput = reasonLabel.querySelector("select");
  reasonInput.disabled = typeof onReasonChange !== "function";
  if (typeof onReasonChange === "function") reasonInput.addEventListener("change", () => onReasonChange(reasonInput.value));
  const resolveCallback = typeof onResolve === "function"
    ? () => onResolve({ winner: winnerSelect.value, reason: reasonInput.value })
    : undefined;
  const previewPanel = titled(c.affectedRecords, [
    element("div", { className: "field-grid conflict-fields" }, [
      element("label", { text: c.selectedWinner }, [winnerSelect]),
      reasonLabel,
    ]),
    preview.length
      ? element("ul", { className: "plain-list" }, preview.map((item) => element("li", { text: item.description || item })))
      : emptyState(c.noResults),
    singleAction(c.resolveConflict, { primary: true, onClick: resolveCallback }),
  ]);
  return page(c.conflictsTitle, c.conflictsLead, [element("div", { className: "columns equal" }, [membersPanel, previewPanel])]);
}

export function renderDatabaseState({
  artifacts = [],
  deletePreview = null,
  importFlow = null,
  housekeeping = {},
  onCreateBackup,
  onDownload,
  onDeleteArtifact,
  onExport,
  onImportPreview,
  onImportCommit,
  onRestore,
  onHousekeeping,
  onRunHousekeeping,
  onHousekeepingDraftChange,
} = {}) {
  const c = copy();
  const artifactRows = artifacts.map((artifact) => [
    identifier(artifact.artifact_id),
    artifact.kind,
    artifact.registered_at,
    artifact.size_bytes,
    identifier(artifact.sha256),
    actionGroup([
      actionButton(c.download, { onClick: callbackFor(onDownload, artifact) }),
      actionButton(c.deleteBackup, { danger: true, onClick: callbackFor(onDeleteArtifact, artifact) }),
    ]),
  ]);
  const backupsPanel = titled(c.databaseIdentity, [
    metaList([[c.identifier, c.unavailable], [c.health, c.unavailable], [c.storage, c.unavailable], [c.compatibility, c.unavailable]]),
    element("div", { className: "row" }, [
      element("h2", { text: c.backups }),
      actionGroup([actionButton(c.createBackup, { primary: true, onClick: onCreateBackup }), actionButton(c.restorePreview, { onClick: onRestore })]),
    ]),
    deletePreview ? element("p", { className: "notice warning", text: `${c.previewDigest}: ${deletePreview.preview_digest}`, attrs: { role: "status" } }) : null,
    dataTable([c.artifact, c.state, c.created, c.size, c.integrity, c.action], artifactRows),
  ]);
  const exportForm = formPanel(c.exportJson, [fieldGrid([
    ...scopeInputs(c),
    labeledInput(c.state, "lifecycleStates", "CANDIDATE,VALIDATED_CURRENT,SUPERSEDED,STALE,QUARANTINED,HISTORICAL,REVOKED,REJECTED"),
    labeledInput(c.maxRecords, "maxRecords", "1000", { type: "number" }), labeledInput(c.identifier, "operationId"),
  ])], c.exportJson, onExport);
  const importForm = formPanel(c.importPreview, [fileInput(c.artifact, "file")], c.importPreview, onImportPreview);
  if (importFlow?.preview) importForm.append(
    element("p", { className: "notice", text: c.importedNeedReview, attrs: { role: "status" } }),
    metaList(Object.entries(importFlow.preview)),
    singleAction(c.importCommit, { primary: true, onClick: onImportCommit }),
  );
  const housekeepingForm = element("form", { className: "panel" });
  const housekeepingFields = fieldGrid([
    ...scopeInputs(c, housekeeping),
    labeledInput(c.maxRecords, "maxRecords", housekeeping.maxRecords ?? "100", { type: "number" }),
    labeledInput(c.requiredFreeBytes, "requiredFreeBytes", housekeeping.requiredFreeBytes ?? "0", { type: "number" }),
  ]);
  housekeepingForm.append(element("h2", { text: c.housekeepingPreview }), housekeepingFields, actionGroup([
    actionButton(c.housekeepingPreview, { onClick: typeof onHousekeeping === "function" ? () => onHousekeeping(formValues(housekeepingForm)) : undefined }),
    actionButton(c.runHousekeeping, { primary: true, onClick: typeof onRunHousekeeping === "function" ? () => onRunHousekeeping(formValues(housekeepingForm)) : undefined }),
  ]));
  housekeepingForm.addEventListener("input", () => onHousekeepingDraftChange?.(formValues(housekeepingForm)));
  return page(c.databaseTitle, c.databaseLead, [backupsPanel, exportForm, importForm, housekeepingForm]);
}

export function renderRestoreState({ restore = {}, onPreview, onAcknowledge, onChallenge, onGrant, onExecute } = {}) {
  const c = copy();
  const upload = formPanel(c.restorePreview, [fileInput(c.artifact, "file"), checkInput(c.trustedOrigin, "trustedOrigin")], c.restorePreview, onPreview);
  if (!restore.authorizationPreview) return page(c.restorePreview, c.restoreLead, [upload]);
  const acknowledge = checkInput(c.restoreAck, "acknowledged", restore.acknowledged);
  acknowledge.querySelector("input")?.addEventListener("change", (event) => onAcknowledge?.(event.target.checked));
  const summary = titled(c.restoreSummary, [
    metaList([[c.identifier, identifier(restore.operationId)], [c.state, restore.status], [c.approval, restore.authorizationPreview], [c.nativeApprovalPhrase, restore.verificationPhrase]]),
    acknowledge,
    actionGroup([
      actionButton(c.requestSensitiveApproval, { onClick: onChallenge }), actionButton(c.checkNativeApproval, { onClick: onGrant }),
      actionButton(restore.recoveryRequired ? c.retry : c.authorize, { primary: true, danger: true, onClick: onExecute }),
    ]),
  ]);
  return page(c.restorePreview, c.restoreLead, [upload, summary]);
}

export function renderAgentsState({ agents = [], guidance = "", onReviewSettings } = {}) {
  const c = copy();
  const rows = agents.map((agent) => [
    agent.runtime || agent.name,
    agent.configured,
    agent.observed,
    agent.lastObserved,
    agent.observationSource,
    agent.pendingReload,
  ]);
  const observations = titled(c.runtimeObservations, [
    dataTable([c.runtime, c.configured, c.observed, c.lastObserved, c.observationSource, c.pendingReload], rows),
  ]);
  const guidancePanel = titled(c.integrationGuidance, [
    guidance ? element("p", { text: guidance }) : emptyState(c.none),
    singleAction(c.reviewConnectionSettings, { onClick: onReviewSettings }),
  ]);
  return page(c.agentsTitle, c.agentsLead, [observations, guidancePanel]);
}

export function renderActivityState({ filters = {}, events = [], diagnostics = {}, onPreviewDiagnostics, onDownloadDiagnostics } = {}) {
  const c = copy();
  const filterPanel = box([
    element("div", { className: "field-grid activity-filters" }, [
      element("label", { text: c.actor }, [element("input", { attrs: { value: filters.actor || "", readonly: "" } })]),
      element("label", { text: c.action }, [element("input", { attrs: { value: filters.action || "", readonly: "" } })]),
      element("label", { text: c.scope }, [element("input", { attrs: { value: filters.scope || "", readonly: "" } })]),
      element("label", { text: c.result }, [element("input", { attrs: { value: filters.result || "", readonly: "" } })]),
      element("label", { text: c.time }, [element("input", { attrs: { value: filters.time || "", readonly: "" } })]),
    ]),
  ]);
  const rows = events.map((event) => [
    event.time,
    event.actor,
    event.action,
    event.scope,
    event.result,
    event.correlation,
  ]);
  const eventPanel = titled(c.recordedEvents, [
    dataTable([c.time, c.actor, c.action, c.scope, c.result, c.correlation], rows),
  ]);
  const diagnosticsPanel = titled(c.diagnostics, [
    diagnostics.description ? element("p", { text: diagnostics.description }) : emptyState(c.none),
    actionGroup([
      actionButton(c.previewDiagnostics, { onClick: onPreviewDiagnostics }),
      actionButton(c.downloadDiagnostics, { onClick: onDownloadDiagnostics }),
    ]),
  ]);
  return page(c.activityTitle, c.activityLead, [filterPanel, eventPanel, diagnosticsPanel]);
}

export function renderSettingsState({
  service = {},
  session = {},
  appearance = {},
  settings = {},
  settingsDraft = {},
  devices = [],
  onStop,
  onRestart,
  onOpenLauncher,
  onEndSession,
  onManageDevice,
  onLanguageChange,
  onThemeChange,
  onSettingsDraftChange,
  onSettingsPreview,
  onSettingsApply,
} = {}) {
  const c = copy();
  const servicePanel = titled(c.service, [
    metaList([
      [c.state, service.state],
      [c.enabledAtBoot, service.enabledAtBoot],
      [c.lastObserved, service.lastObserved],
      [c.source, service.observationSource],
    ]),
    service.explanation ? element("p", { className: "muted", text: service.explanation }) : null,
    actionGroup([
      actionButton(c.stopUntilReboot, { danger: true, onClick: onStop }),
      actionButton(c.restartWebui, { onClick: onRestart }),
      actionButton(c.openLauncher, { onClick: onOpenLauncher }),
    ]),
  ]);
  const deviceRows = devices.map((device) => [
    device.name || device.id,
    device.state,
    device.lastObserved,
    actionButton(c.manageDevice, { onClick: callbackFor(onManageDevice, device) }),
  ]);
  const languageSelect = element("select", { attrs: { name: "language" } });
  for (const value of appearance.availableLanguages || []) {
    const option = element("option", { text: value, attrs: { value } });
    if (value === appearance.language) option.selected = true;
    languageSelect.append(option);
  }
  languageSelect.disabled = typeof onLanguageChange !== "function";
  if (typeof onLanguageChange === "function") languageSelect.addEventListener("change", () => onLanguageChange(languageSelect.value));
  const themeSelect = element("select", { attrs: { name: "theme" } });
  for (const value of appearance.availableThemes || []) {
    const option = element("option", { text: value, attrs: { value } });
    if (value === appearance.theme) option.selected = true;
    themeSelect.append(option);
  }
  themeSelect.disabled = typeof onThemeChange !== "function";
  if (typeof onThemeChange === "function") themeSelect.addEventListener("change", () => onThemeChange(themeSelect.value));
  const sessionPanel = titled(c.sessionAndDevices, [
    metaList([[c.inactivity, session.inactivity], [c.maximumAge, session.maximumAge]]),
    element("h3", { text: c.appearance }),
    fieldGrid([
      element("label", { text: c.language }, [languageSelect]),
      element("label", { text: c.theme }, [themeSelect]),
    ]),
    singleAction(c.endSession, { onClick: onEndSession }),
    dataTable([c.device, c.state, c.lastObserved, c.action], deviceRows),
  ]);
  const settingsForm = element("form", { className: "panel" });
  settingsForm.append(element("h2", { text: c.editableSettings }));
  const settingFields = element("div", { className: "field-grid" });
  for (const name of settings.allowlist || []) {
    const baseline = Object.hasOwn(settings.configured || {}, name) ? settings.configured?.[name] : settings.effective?.[name];
    const current = Object.hasOwn(settingsDraft, name) ? settingsDraft[name] : baseline;
    settingFields.append(labeledInput(name, `setting:${name}`, current ?? ""));
  }
  settingsForm.append(settingFields, actionGroup([
    actionButton(c.previewChanges, { onClick: typeof onSettingsPreview === "function" ? () => onSettingsPreview(formValues(settingsForm)) : undefined }),
    actionButton(c.applyChanges, { primary: true, onClick: typeof onSettingsApply === "function" ? () => onSettingsApply(formValues(settingsForm)) : undefined }),
  ]));
  settingsForm.addEventListener("input", () => onSettingsDraftChange?.(formValues(settingsForm)));
  return page(c.settingsTitle, c.settingsLead, [element("div", { className: "columns" }, [servicePanel, sessionPanel]), settingsForm]);
}

export function renderPurgeRecoveryState({ purge = {}, onTypedChange, onChallenge, onGrant, onExecute } = {}) {
  const c = copy();
  const preview = purge.purgePreview || {};
  const targetRows = (preview.targets || []).map((target) => [
    target.scope,
    target.project_id || c.none,
    target.worktree_id || c.none,
    target.workflow_id || c.none,
    target.agent_id || c.none,
    identifier(target.memory_id),
    identifier(target.record_id),
    target.revision,
  ]);
  const typed = labeledInput(c.typedConfirmation, "typedConfirmation", purge.typedInput || "");
  typed.querySelector("input")?.addEventListener("input", (event) => onTypedChange?.(event.target.value));
  const authorization = element("section", { className: "panel purge-panel" }, [
    element("h2", { text: c.purgeTitle }),
    preview.backup_retention_warning ? element("p", { className: "notice danger", text: preview.backup_retention_warning, attrs: { role: "alert" } }) : null,
    metaList([
      [c.identifier, purge.operationId ? identifier(purge.operationId) : null],
      [c.previewDigest, purge.previewDigest ? identifier(purge.previewDigest) : null],
      [c.expectedConfirmation, purge.typedConfirmation],
      [c.state, purge.status],
    ]),
    targetRows.length
      ? dataTable([c.scope, c.projectId, c.worktreeId, c.workflowId, c.agentId, "memory_id", "record_id", "revision"], targetRows, c.purgePreview)
      : emptyState(c.none),
    typed,
    purge.verificationPhrase
      ? metaList([[c.nativeApprovalPhrase, purge.verificationPhrase]])
      : null,
    actionGroup([
      actionButton(c.requestSensitiveApproval, { onClick: onChallenge }),
      actionButton(c.checkNativeApproval, { onClick: onGrant }),
      actionButton(purge.recoveryRequired ? c.retryPurge : c.commitPurge, { primary: true, danger: true, onClick: onExecute }),
    ]),
  ].filter(Boolean));
  return authorization;
}

export function renderRecoveryState({ message = "", steps = [], purge = null, onResume, onPurgeTypedChange, onPurgeChallenge, onPurgeGrant, onPurgeExecute } = {}) {
  const c = copy();
  const notice = message ? element("div", { className: "notice danger", text: message, attrs: { role: "alert" } }) : null;
  const list = element("ol", { className: "steps" }, steps.map((step, index) =>
    element("li", {}, [
      element("span", { className: "step-num", text: index + 1, attrs: { "aria-hidden": "true" } }),
      element("div", {}, [element("strong", { text: step.title || step }), step.detail ? element("p", { className: "muted small", text: step.detail }) : null]),
    ])));
  const panel = titled(c.recoverySteps, [
    steps.length ? list : emptyState(c.none),
    element("div", { className: "page-actions" }, [actionButton(c.resume, { primary: true, onClick: onResume })]),
  ]);
  const purgePanel = purge ? renderPurgeRecoveryState({
    purge,
    onTypedChange: onPurgeTypedChange,
    onChallenge: onPurgeChallenge,
    onGrant: onPurgeGrant,
    onExecute: onPurgeExecute,
  }) : null;
  return page(c.recoveryTitle, c.recoveryLead, [notice, purgePanel, panel].filter(Boolean));
}

export function renderConnectState({ request = {}, locator = "", onLocatorChange, onClaim, onCancel } = {}) {
  const c = copy();
  const words = Array.isArray(request.phrase) ? request.phrase.filter(Boolean) : [];
  const phrase = words.length
    ? element("div", { className: "phrase", attrs: { "aria-label": c.approval } }, words.map((word) => element("span", { text: word })))
    : emptyState(c.waiting);
  const panel = element("section", { className: "panel connect-panel" }, [
    element("h2", { text: c.approval }),
    element("p", { className: "notice", text: c.launcherApproval }),
    phrase,
    metaList([[c.identifier, identifier(request.id)], [c.state, request.status]]),
  ]);
  const form = element("form", { className: "connect-form" }, [
    labeledInput(c.requestLocator, "locator", locator),
    actionGroup([
      actionButton(c.claimRequest, { primary: true, onClick: typeof onClaim === "function" ? () => form.requestSubmit() : undefined }),
      actionButton(c.cancel, { onClick: onCancel }),
    ]),
  ]);
  form.addEventListener("input", () => onLocatorChange?.(form.elements.locator?.value || ""));
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    onClaim?.(form.elements.locator?.value || "");
  });
  panel.append(form);
  return page(c.connectTitle, c.connectLead, [panel]);
}

export const stateRenderers = {
  overview: renderOverviewState,
  browse: renderBrowseState,
  list: renderListState,
  detail: renderDetailState,
  review: renderReviewState,
  conflicts: renderConflictsState,
  database: renderDatabaseState,
  restore: renderRestoreState,
  agents: renderAgentsState,
  activity: renderActivityState,
  settings: renderSettingsState,
  recovery: renderRecoveryState,
  connect: renderConnectState,
};
