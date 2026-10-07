export type HomeCopy = {
  meta: {
    title: string;
    description: string;
    structuredDataDescription: string;
  };
  hero: {
    kicker: string;
    title: string;
    titleMuted: string;
    lead: string;
    primaryCta: string;
    secondaryCta: string;
    proofsLabel: string;
    proofs: Array<{strong: string; rest: string}>;
    developerLabel: string;
    developerLink: string;
    windowTitle: string;
    localBadge: string;
    promptLabel: string;
    prompt: string;
    verifiedLabel: string;
    currentBadge: string;
    record: string;
    scope: string;
    source: string;
    laterLabel: string;
    later: string;
    caption: string;
  };
  useCases: {
    eyebrow: string;
    title: string;
    intro: string;
    items: Array<{eyebrow: string; title: string; quote: string; note: string}>;
    link: string;
  };
  everydayFit: {
    eyebrow: string;
    title: string;
    intro: string;
    cards: Array<{title: string; body: string}>;
    beginnerCta: string;
    setupCta: string;
    faqCta: string;
  };
  whyGoverned: {
    eyebrow: string;
    title: string;
    intro: string;
    cards: Array<{title: string; body: string}>;
  };
  lifecycle: {
    eyebrow: string;
    title: string;
    intro: string;
    steps: Array<{label: string; title: string; body: string}>;
    link: string;
  };
  authority: {
    eyebrow: string;
    title: string;
    intro: string;
    higherAuthority: string;
    trueNow: string;
    trueNowItems: string[];
    outranks: string;
    supportingEvidence: string;
    factLaneMemory: string;
    memoryBody: string;
    realLifeTitle: string;
    realLifeParagraphs: string[];
  };
  quickStart: {
    eyebrow: string;
    title: string;
    intro: string;
    panelTitle: string;
    body: string;
    cta: string;
  };
  boundaries: {
    eyebrow: string;
    title: string;
    intro: string;
    supportedLabel: string;
    supportedItems: string[];
    limitsLabel: string;
    limitItems: string[];
    link: string;
  };
  tools: {
    eyebrow: string;
    title: string;
    intro: string;
    items: Array<[string, string]>;
    link: string;
  };
  architecture: {
    eyebrow: string;
    title: string;
    intro: string;
    noteBodies: string[];
    link: string;
  };
  finalCta: {
    eyebrow: string;
    title: string;
    fitCta: string;
    setupCta: string;
    githubCta: string;
  };
};

export const homeCopy: Record<'en' | 'ar', HomeCopy> = {
  en: {
    meta: {
      title: 'Governed MCP memory for AI agents',
      description:
        'FactLane is a free, local-first MCP memory layer for AI agents: bounded reusable facts with provenance, freshness and a Candidate-to-Current trust boundary.',
      structuredDataDescription:
        'Free, local-first governed MCP memory for compatible AI agents, built around bounded reusable facts with exact scope, provenance, freshness and trusted Candidate-to-Current verification.',
    },
    hero: {
      kicker: 'Free · local-first · governed memory for AI agents',
      title: 'What should your agent remember?',
      titleMuted: ' And what should it never treat as truth?',
      lead:
        'FactLane adds a small governed fact lane beside the memory you already use. Keep useful preferences, project facts and workflow rules across sessions with scope, provenance, freshness and Candidate → Current verification.',
      primaryCta: 'See if FactLane fits',
      secondaryCta: 'Give it to your agent',
      proofsLabel: 'FactLane at a glance',
      proofs: [
        {strong: 'Apache-2.0', rest: ' open source'},
        {strong: 'v0.1.3', rest: ' production-qualified local profile'},
        {strong: 'Five tools', rest: ' focused MCP surface'},
      ],
      developerLabel: 'Prefer manual setup?',
      developerLink: 'Open the Quick Start →',
      windowTitle: 'FactLane · governed facts',
      localBadge: 'LOCAL',
      promptLabel: 'You',
      prompt:
        'Remember that I prefer concise answers in Arabic, but keep technical terms in English.',
      verifiedLabel: 'After trusted verification · Preference',
      currentBadge: 'CURRENT',
      record: 'Concise Arabic explanations + English technical terms',
      scope: 'scope · global user',
      source: 'source · user instruction',
      laterLabel: 'Next week',
      later:
        'Your assistant can reuse the approved preference without rebuilding the context from scratch.',
      caption: 'Share facts. Not context.',
    },
    useCases: {
      eyebrow: 'What belongs in the governed lane?',
      title: 'Keep the facts that should survive a session.',
      intro:
        'FactLane is strongest when a small fact will matter later and you want to know where it came from, how fresh it is, and whether it is actually Current.',
      items: [
        {
          eyebrow: 'Project state',
          title: 'Carry forward facts that change how work should continue.',
          quote: '“The release is v0.1.3. Production changes still require owner approval.”',
          note: 'Current retrieval can reuse the verified fact without replaying the project history.',
        },
        {
          eyebrow: 'Stable preferences',
          title: 'Remember durable working preferences without making them absolute.',
          quote: '“Keep answers concise unless I explicitly ask for a deep explanation.”',
          note: 'The preference can survive the session while a new current instruction still wins.',
        },
        {
          eyebrow: 'Workflow boundaries',
          title: 'Preserve rules that agents need to see again.',
          quote: '“Production changes need approval. Local tests can run automatically.”',
          note: 'A bounded fact can be reused across compatible hosts without turning the memory store into the authority that grants execution rights.',
        },
      ],
      link: 'See more use cases →',
    },
    everydayFit: {
      eyebrow: 'What using it actually means',
      title: 'FactLane is not another chatbot. It is the memory layer behind one.',
      intro:
        'Once a compatible assistant is connected, your day-to-day experience can stay simple: keep talking to the assistant you already use while useful approved facts remain available between sessions.',
      cards: [
        {
          title: 'You keep using your assistant.',
          body: 'FactLane does not replace the chat or coding assistant. It gives a compatible host a local place to keep useful facts.',
        },
        {
          title: 'The useful fact survives the session.',
          body: 'Preferences, project facts and recurring rules can be reused later without replaying the entire old conversation.',
        },
        {
          title: 'Setup today is still technical.',
          body: 'FactLane v0.1.3 runs locally through a compatible MCP host. Someone must configure that connection, even though normal use afterward does not require thinking about databases or vector search.',
        },
      ],
      beginnerCta: "I'm new — explain it simply",
      setupCta: "I'm ready for technical setup",
      faqCta: 'Not sure yet? Read the FAQ →',
    },
    whyGoverned: {
      eyebrow: 'Why not just remember everything?',
      title: 'Remembering is useful. Blindly trusting old memory is not.',
      intro:
        'Preferences change. Projects move forward. A suggestion can be wrong. FactLane keeps memory useful without letting it silently overrule what is true now.',
      cards: [
        {
          title: 'Old information can become stale.',
          body: '“The release is v0.1.2” might have been true last month and wrong today.',
        },
        {
          title: 'A new suggestion is not automatically trusted.',
          body: 'An assistant can propose something worth remembering without being allowed to declare it current truth.',
        },
        {
          title: 'What is true now should win.',
          body: 'Current user instructions, live project state and verified live sources outrank remembered facts.',
        },
      ],
    },
    lifecycle: {
      eyebrow: 'A simple mental model',
      title: 'A fact can be worth remembering before it is trusted.',
      intro:
        'FactLane separates “save this for later” from “this is current and verified.” That is the core idea behind Candidate → Current.',
      steps: [
        {
          label: 'Something useful appears',
          title: 'Your assistant proposes a fact.',
          body: 'Example: “This project deploys only after owner approval.”',
        },
        {
          label: 'Candidate',
          title: 'Worth remembering, not trusted yet.',
          body: 'The assistant cannot promote its own contribution just by claiming authority.',
        },
        {
          label: 'Trusted verification',
          title: 'A separate trusted step checks it.',
          body: 'Revision and identity checks prevent stale or mismatched promotion.',
        },
        {
          label: 'Current',
          title: 'Verified and eligible to use now.',
          body: 'Normal current-state retrieval excludes unverified Candidates.',
        },
      ],
      link: 'Understand Candidate → Current →',
    },
    authority: {
      eyebrow: 'The important boundary',
      title: 'Memory supports the decision. It does not own the decision.',
      intro: 'FactLane remembers what was established, but current reality still has the final say.',
      higherAuthority: 'Higher authority',
      trueNow: 'What is true now',
      trueNowItems: [
        'Current user instruction',
        'Live repository or product state',
        'Verified live source',
      ],
      outranks: 'outranks ↓',
      supportingEvidence: 'Supporting evidence',
      factLaneMemory: 'FactLane memory',
      memoryBody: 'Durable, scoped and provenance-aware — but still memory.',
      realLifeTitle: 'Why this matters in real life',
      realLifeParagraphs: [
        'Suppose FactLane remembers that you prefer short answers. That is useful tomorrow. But if you say “go deep on this one,” your current instruction wins.',
        'The same principle applies to project versions, deployment rules, ownership, environment state and other facts that can change over time.',
      ],
    },
    quickStart: {
      eyebrow: 'Ready to try it?',
      title: 'Start local. Keep the first run boring.',
      intro:
        'Use the exact v0.1.3 release, verify the linked SQLite runtime, install a supported local embedding model, then connect your MCP host.',
      panelTitle: 'New to the technical setup?',
      body: 'The Quick Start explains the prerequisites one step at a time, including what launches FactLane and why “five tools discovered” does not automatically mean “writes are allowed.”',
      cta: 'Follow the Quick Start',
    },
    boundaries: {
      eyebrow: 'A bounded production claim',
      title: 'Production-qualified for one documented local profile — not marketed as universal infrastructure.',
      intro:
        'FactLane v0.1.3 is the first official production release. The support statement is intentionally specific enough to verify and narrow enough to be honest.',
      supportedLabel: 'Supported profile',
      supportedItems: [
        'Python 3.11+',
        'linked SQLite 3.42.0+',
        'command-launched stdio MCP',
        'supported local Ollama embeddings',
        'documented local POSIX storage/recovery contract',
      ],
      limitsLabel: 'Current limits',
      limitItems: [
        'no remote embedding endpoint fallback',
        'no HTTP/SSE/Streamable HTTP server transport',
        'not a transcript archive or general document crawler',
        'facts are bounded to 2,000 UTF-8 bytes',
        'language/semantic quality remains workload-specific',
      ],
      link: 'Read environment and compatibility →',
    },
    tools: {
      eyebrow: 'For developers',
      title: 'Five focused MCP tools.',
      intro:
        'The public surface stays intentionally small. Tool visibility does not itself grant write authority.',
      items: [
        ['memory_search', 'Find eligible facts inside one exact scope.'],
        ['memory_get', 'Inspect one memory and its provenance or revision details.'],
        ['memory_store', 'Contribute one bounded fact when the launcher allows it.'],
        ['memory_update', 'Reverify or replace under trusted revision rules.'],
        ['memory_status', 'Inspect bounded storage and embedding-profile health.'],
      ],
      link: 'Read the tool guide →',
    },
    architecture: {
      eyebrow: 'Local-first architecture',
      title: 'Runs beside your assistants, not as a remote memory cloud.',
      intro:
        'The current supported profile is a command-launched stdio MCP server with local embeddings and local SQLite/SQLite-vec storage.',
      noteBodies: [
        'No HTTP, SSE or Streamable HTTP server transport in v0.1.3.',
        'The shipped provider uses Ollama over loopback HTTP.',
        'The storage backend does not decide who is allowed to promote memory.',
      ],
      link: 'Read the architecture →',
    },
    finalCta: {
      eyebrow: 'A governed lane for reusable facts',
      title: 'Keep your broad memory. Add a governed fact lane.',
      fitCta: 'Start with your agent',
      setupCta: 'Manual setup',
      githubCta: 'View the source on GitHub ↗',
    },
  },
  ar: {
    meta: {
      title: 'ذاكرة MCP محكومة لوكلاء الذكاء الاصطناعي',
      description:
        'FactLane طبقة ذاكرة MCP مجانية ومحلية لوكلاء الذكاء الاصطناعي: حقائق محددة قابلة لإعادة الاستخدام مع نطاق ومصدر وحداثة وفصل واضح بين Candidate وCurrent.',
      structuredDataDescription:
        'ذاكرة MCP محكومة ومجانية ومحلية لوكلاء الذكاء الاصطناعي المتوافقين، مبنية على حقائق محددة قابلة لإعادة الاستخدام مع نطاق ومصدر وحداثة وتحقّق موثوق بين Candidate وCurrent.',
    },
    hero: {
      kicker: 'مجاني · محلي · ذاكرة محكومة لوكلاء الذكاء الاصطناعي',
      title: 'ما الذي يستحق أن يتذكره وكيلك؟',
      titleMuted: ' وما الذي يجب ألا يعامله كحقيقة؟',
      lead:
        'يضيف FactLane مسارًا صغيرًا ومحكومًا للحقائق بجانب الذاكرة التي تستخدمها بالفعل. احتفظ بالتفضيلات وحقائق المشروع وقواعد العمل بين الجلسات مع scope ومصدر وحداثة وتحقّق Candidate → Current.',
      primaryCta: 'اعرف هل FactLane مناسب لك',
      secondaryCta: 'أعطه لوكيلك',
      proofsLabel: 'FactLane باختصار',
      proofs: [
        {strong: 'Apache-2.0', rest: ' مفتوح المصدر'},
        {strong: 'v0.1.3', rest: ' ملف محلي مؤهل إنتاجيًا'},
        {strong: 'خمس أدوات', rest: ' سطح MCP مركز'},
      ],
      developerLabel: 'تفضّل الإعداد بنفسك؟',
      developerLink: 'افتح Quick Start للمطور',
      windowTitle: 'FactLane · حقائق محكومة',
      localBadge: 'LOCAL',
      promptLabel: 'أنت',
      prompt: 'تذكّر أنني أفضّل إجابات عربية مختصرة، مع إبقاء المصطلحات التقنية بالإنجليزية.',
      verifiedLabel: 'بعد تحقّق موثوق · تفضيل',
      currentBadge: 'CURRENT',
      record: 'شرح عربي مختصر + مصطلحات تقنية بالإنجليزية',
      scope: 'scope · global user',
      source: 'source · user instruction',
      laterLabel: 'الأسبوع القادم',
      later:
        'يمكن لمساعدك إعادة استخدام التفضيل المعتمد من دون إعادة بناء السياق من الصفر.',
      caption: 'شارك الحقائق، لا السياق.',
    },
    useCases: {
      eyebrow: 'ما الذي يستحق المسار المحكوم؟',
      title: 'احتفظ بالحقائق التي يجب أن تعبر الجلسات.',
      intro:
        'تظهر قيمة FactLane عندما تكون هناك حقيقة صغيرة ستؤثر في العمل لاحقًا وتريد معرفة مصدرها وحداثتها وهل أصبحت Current فعلًا.',
      items: [
        {
          eyebrow: 'حالة المشروع',
          title: 'انقل الحقائق التي تغيّر كيف يجب أن يستمر العمل.',
          quote: '«الإصدار الحالي v0.1.3. تغييرات الإنتاج ما زالت تحتاج موافقة المالك.»',
          note: 'يمكن للاسترجاع الحالي إعادة استخدام الحقيقة المتحقَّق منها من دون إعادة تاريخ المشروع كله.',
        },
        {
          eyebrow: 'تفضيلات مستقرة',
          title: 'تذكّر تفضيلات العمل من دون تحويلها إلى أوامر مطلقة.',
          quote: '«اجعل الإجابات مختصرة إلا إذا طلبت منك صراحةً شرحًا عميقًا.»',
          note: 'يبقى التفضيل عبر الجلسات، لكن تعليماتك الحالية الجديدة تظل أعلى منه.',
        },
        {
          eyebrow: 'حدود سير العمل',
          title: 'أبقِ القواعد التي يحتاج الوكلاء إلى رؤيتها مرة أخرى.',
          quote: '«تغييرات بيئة الإنتاج تحتاج موافقة. الاختبارات المحلية يمكن تشغيلها تلقائيًا.»',
          note: 'تُعاد استخدام الحقيقة عبر hosts متوافقة من دون أن يصبح مخزن الذاكرة هو الجهة التي تمنح صلاحية التنفيذ.',
        },
      ],
      link: 'شاهد حالات استخدام إضافية',
    },
    everydayFit: {
      eyebrow: 'بعد الإعداد',
      title: 'أضف الذاكرة إلى المساعد الذي تستخدمه بالفعل.',
      intro:
        'FactLane ليس chatbot مستقلًا ولا يطلب منك تغيير طريقة عملك اليومية. هو طبقة ذاكرة خلف host متوافق؛ أنت تستمر في استخدام وكيلك، والحقائق المؤهلة تبقى متاحة بين الجلسات.',
      cards: [
        {
          title: 'لا تغيّر وكيلك المعتاد.',
          body: 'FactLane يعمل خلف host متوافق ويمنحه مكانًا محليًا لإعادة استخدام الحقائق النافعة؛ لا يستبدل مساعد المحادثة أو البرمجة.',
        },
        {
          title: 'تحتفظ بالحقيقة، لا المحادثة.',
          body: 'التفضيلات وحقائق المشروع والقواعد المتكررة يمكن أن تبقى كذكريات محددة ومسنَدة، بدل تخزين transcript كامل.',
        },
        {
          title: 'وكيل قادر يمكنه تنفيذ جزء كبير من الإعداد معك.',
          body: 'إذا كان وكيلك يملك صلاحية Terminal والملفات وإعداد MCP host، يمكنه فحص البيئة وتنفيذ خطوات التثبيت والتهيئة المدعومة. وإذا لم يملكها، يظل قادرًا على الشرح والإرشاد فقط.',
        },
      ],
      beginnerCta: 'ابدأ مع وكيلك',
      setupCta: 'الإعداد اليدوي للمطور',
      faqCta: 'ما زلت تقيّم الملاءمة؟ اقرأ الأسئلة الشائعة',
    },
    whyGoverned: {
      eyebrow: 'لماذا لا نتذكّر كل شيء فحسب؟',
      title: 'التذكّر مفيد. الثقة العمياء في ذاكرة قديمة ليست كذلك.',
      intro:
        'التفضيلات تتغير، والمشروعات تتقدم، والاقتراح قد يكون خاطئًا. يحافظ FactLane على فائدة الذاكرة من دون أن يسمح لها بتجاوز الحقيقة الحالية بصمت.',
      cards: [
        {
          title: 'المعلومة القديمة قد تصبح غير صالحة.',
          body: 'قد تكون عبارة «الإصدار هو v0.1.2» صحيحة الشهر الماضي وخاطئة اليوم.',
        },
        {
          title: 'الاقتراح الجديد ليس موثوقًا تلقائيًا.',
          body: 'يمكن للمساعد اقتراح شيء يستحق التذكّر من دون أن يملك صلاحية إعلانه حقيقة حالية.',
        },
        {
          title: 'ما هو صحيح الآن يجب أن يفوز.',
          body: 'تعليمات المستخدم الحالية، والحالة الحية للمشروع، والمصادر الحية المتحقَّق منها تتقدم على الحقائق المتذكَّرة.',
        },
      ],
    },
    lifecycle: {
      eyebrow: 'نموذج ذهني بسيط',
      title: 'قد تستحق الحقيقة أن تُحفَظ قبل أن تصبح موثوقة.',
      intro:
        'يفصل FactLane بين «احفظ هذا لوقت لاحق» و«هذه حقيقة حالية ومتحقَّق منها». هذه هي فكرة الانتقال من Candidate إلى Current.',
      steps: [
        {
          label: 'ظهرت معلومة مفيدة',
          title: 'يقترح مساعدك حقيقة.',
          body: 'مثال: «لا يتم Deploy لهذا المشروع إلا بعد موافقة المالك.»',
        },
        {
          label: 'Candidate',
          title: 'تستحق التذكّر، لكنها ليست موثوقة بعد.',
          body: 'لا يستطيع المساعد ترقية مساهمته بنفسه بمجرد ادعاء صلاحية التحقّق.',
        },
        {
          label: 'Trusted verification',
          title: 'خطوة مستقلة وموثوقة تتحقق منها.',
          body: 'فحوص revision وidentity تمنع ترقية معلومة قديمة أو غير متطابقة.',
        },
        {
          label: 'Current',
          title: 'متحقَّق منها ومؤهلة للاستخدام الآن.',
          body: 'الاسترجاع العادي للحالة الحالية يستبعد Candidates غير المتحقَّق منها.',
        },
      ],
      link: 'افهم الانتقال من Candidate إلى Current',
    },
    authority: {
      eyebrow: 'الحد الفاصل المهم',
      title: 'الذاكرة تدعم القرار. لكنها لا تملك القرار.',
      intro: 'يتذكّر FactLane ما تم إثباته، لكن الواقع الحالي يظل صاحب الكلمة الأخيرة.',
      higherAuthority: 'مرجعية أعلى',
      trueNow: 'ما هو صحيح الآن',
      trueNowItems: [
        'تعليمات المستخدم الحالية',
        'الحالة الحية للمستودع أو المنتج',
        'مصدر حي متحقَّق منه',
      ],
      outranks: 'مرجعيته أعلى من',
      supportingEvidence: 'دليل داعم',
      factLaneMemory: 'ذاكرة FactLane',
      memoryBody: 'دائمة، محددة النطاق، ومرتبطة بمصدرها — لكنها تظل ذاكرة.',
      realLifeTitle: 'لماذا يهم هذا في الاستخدام الحقيقي؟',
      realLifeParagraphs: [
        'لنفترض أن FactLane يتذكّر أنك تفضّل الإجابات القصيرة. هذا مفيد غدًا، لكن إذا قلت «تعمّق في هذه المرة»، فتعليماتك الحالية هي التي تفوز.',
        'المبدأ نفسه ينطبق على إصدارات المشروع، وقواعد النشر، والملكية، وحالة البيئة، وغيرها من الحقائق التي تتغير بمرور الوقت.',
      ],
    },
    quickStart: {
      eyebrow: 'جاهز للتجربة؟',
      title: 'ابدأ محليًا. واجعل التشغيل الأول بسيطًا ومملًا.',
      intro:
        'استخدم إصدار v0.1.3 المحدد، وتحقق من SQLite المرتبط، وثبّت embedding model محليًا ومدعومًا، ثم صِل MCP host.',
      panelTitle: 'جديد على الإعداد التقني؟',
      body: 'يشرح Quick Start المتطلبات خطوة بخطوة، بما في ذلك ما الذي يشغّل FactLane ولماذا ظهور «خمس أدوات» لا يعني تلقائيًا أن الكتابة مسموحة.',
      cta: 'اتبع Quick Start',
    },
    boundaries: {
      eyebrow: 'ادعاء إنتاجي محدد',
      title: 'مؤهل إنتاجيًا لملف محلي موثق — وليس ادعاءً بأننا بنية تحتية مناسبة لكل بيئة.',
      intro:
        'FactLane v0.1.3 هو أول إصدار إنتاج رسمي. تعمدنا أن يكون نطاق الدعم محددًا بما يكفي لإثباته وصريحًا بما يكفي لعدم المبالغة.',
      supportedLabel: 'ملف التشغيل المدعوم',
      supportedItems: [
        'Python 3.11+',
        'SQLite 3.42.0+ مرتبط فعليًا',
        'command-launched stdio MCP',
        'Ollama embeddings محلية ومدعومة',
        'عقد POSIX محلي موثّق للتخزين والاستعادة',
      ],
      limitsLabel: 'الحدود الحالية',
      limitItems: [
        'لا يوجد fallback إلى remote embedding endpoint',
        'لا يوجد HTTP/SSE/Streamable HTTP server transport',
        'ليس transcript archive ولا document crawler عامًا',
        'كل حقيقة محدودة إلى 2,000 UTF-8 bytes',
        'جودة اللغة والدلالة تعتمد على نوع العمل',
      ],
      link: 'اقرأ متطلبات البيئة والتوافق',
    },
    tools: {
      eyebrow: 'للمطورين',
      title: 'خمس أدوات MCP فقط على السطح العام.',
      intro:
        'الواجهة العامة صغيرة عمدًا. ظهور memory_store أو memory_update لا يمنح الوكيل صلاحية الكتابة؛ صلاحية التنفيذ تأتي من إعداد الـlauncher الموثوق.',
      items: [
        ['memory_search', 'ابحث عن الحقائق المؤهلة داخل scope محدد بدقة.'],
        ['memory_get', 'افحص حقيقة واحدة مع provenance أو تفاصيل revision الخاصة بها.'],
        ['memory_store', 'ساهم بحقيقة محدودة عندما يسمح launcher بذلك.'],
        ['memory_update', 'نفّذ reverify أو replace وفق قواعد revision موثوقة.'],
        ['memory_status', 'افحص صحة التخزين وembedding profile ضمن الحدود المدعومة.'],
      ],
      link: 'اقرأ دليل الأدوات',
    },
    architecture: {
      eyebrow: 'لمن يريد التفاصيل التقنية',
      title: 'محلي بجوار وكلائك، لا خدمة ذاكرة سحابية بعيدة.',
      intro:
        'ملف التشغيل المدعوم حاليًا هو stdio MCP server يُشغَّل بالأمر، مع embeddings محلية وتخزين محلي SQLite/SQLite-vec.',
      noteBodies: [
        'لا يوجد HTTP أو SSE أو Streamable HTTP server transport في v0.1.3.',
        'الـprovider المرفق يستخدم Ollama عبر loopback HTTP.',
        'طبقة التخزين لا تقرر من يملك صلاحية ترقية الذاكرة.',
      ],
      link: 'اقرأ المعمارية',
    },
    finalCta: {
      eyebrow: 'مسار محكوم للحقائق القابلة لإعادة الاستخدام',
      title: 'أبقِ ذاكرتك الواسعة. وأضف مسارًا محكومًا للحقائق.',
      fitCta: 'ابدأ مع وكيلك',
      setupCta: 'الإعداد اليدوي للمطور',
      githubCta: 'اعرض المشروع على GitHub',
    },
  },
};

export type LandingExperience = {
  fitCheck: {
    eyebrow: string;
    title: string;
    intro: string;
    items: Array<{title: string; body: string}>;
    boundary: string;
    action: string;
  };
  differentiation: {
    eyebrow: string;
    title: string;
    intro: string;
    cards: Array<{title: string; body: string}>;
    docsAction: string;
  };
  coexistence: {
    eyebrow: string;
    title: string;
    intro: string;
    lanes: Array<{label: string; title: string; body: string}>;
    note: string;
    docsAction: string;
  };
  onboarding: {
    eyebrow: string;
    title: string;
    intro: string;
    capabilityTitle: string;
    capabilityBody: string;
    steps: Array<{title: string; body: string}>;
    promptLabel: string;
    promptTitle: string;
    promptBody: string;
    copyAction: string;
    copiedAction: string;
    copyFailedAction: string;
    githubAction: string;
    manualAction: string;
    promptDirection: 'ltr' | 'rtl';
    badge: string;
  };
  trust: {
    eyebrow: string;
    title: string;
    intro: string;
    cards: Array<{title: string; body: string}>;
    flow: Array<{label: string; body: string}>;
    docsAction: string;
  };
  rigor: {
    eyebrow: string;
    title: string;
    intro: string;
    cards: Array<{title: string; body: string}>;
    qualification: string;
    architectureAction: string;
    securityAction: string;
  };
  whyNow: {
    eyebrow: string;
    title: string;
    body: string;
    caveat: string;
  };
};

export const landingExperience: Record<'en' | 'ar', LandingExperience> = {
  en: {
    fitCheck: {
      eyebrow: 'Quick fit check',
      title: 'Is this a FactLane problem?',
      intro:
        'FactLane is useful when a small fact should survive a session, but you still need to know where it came from, how fresh it is and whether it is trusted as Current.',
      items: [
        {
          title: 'Your agent keeps relearning the same small facts.',
          body: 'Stable preferences, project facts and workflow rules matter again next session, but replaying the old conversation is wasteful.',
        },
        {
          title: 'Remembered facts can become stale or misleading.',
          body: 'You want provenance, freshness and an explicit Candidate → Current lifecycle instead of treating every saved fact as equally trustworthy.',
        },
        {
          title: 'You want memory without giving memory authority.',
          body: 'Current instructions, live project state and verified live sources must still outrank anything the agent remembers.',
        },
      ],
      boundary:
        'If you mainly want a full conversation archive, broad personal memory or a large knowledge graph, keep that system. FactLane is the narrower governed lane beside it.',
      action: 'See the concrete use cases →',
    },
    differentiation: {
      eyebrow: 'What is FactLane?',
      title: 'A governed fact layer for AI agents that need memory across sessions.',
      intro:
        'FactLane is a free, open-source, local-first MCP memory layer for compatible AI-agent hosts. It keeps small reusable facts with scope, provenance and freshness; a contribution can be a Candidate without becoming verified Current state, and current instructions, live project state or verified live sources still win.',
      cards: [
        {
          title: 'What problem does it solve?',
          body: 'An agent can reuse a useful preference, project fact or workflow rule without replaying the old conversation — and without treating stale memory as current truth.',
        },
        {
          title: 'How is it different from built-in memory?',
          body: 'Broad or native memory is good for rich recall and continuity. FactLane is the narrower governed lane for facts that need exact scope, provenance, freshness and Candidate → Current verification.',
        },
        {
          title: 'What does it not replace?',
          body: 'It does not replace your assistant, broad memory, a knowledge base or execution authority. It sits beside them and governs a small class of reusable facts.',
        },
      ],
      docsAction: 'Read the core concepts →',
    },
    coexistence: {
      eyebrow: 'Complement, do not replace',
      title: 'Keep your broad memory. Add FactLane as the governed fact lane.',
      intro:
        'Integrated memory is not one problem with one correct architecture. Different layers are good at different jobs.',
      lanes: [
        {
          label: 'Broad / native memory',
          title: 'Rich recall and continuity',
          body: 'Useful for preferences, summaries, long-lived context and the broad personal or agent memory experience.',
        },
        {
          label: 'Knowledge graphs / wikis',
          title: 'Connected knowledge at scale',
          body: 'Useful for relationships, documents, concepts and large bodies of organized knowledge.',
        },
        {
          label: 'FactLane',
          title: 'A governed fact lane',
          body: 'Useful when a small fact needs exact scope, provenance, freshness, Candidate → Current verification and explicit authority boundaries.',
        },
      ],
      note:
        'Codex and Hermes are tested stdio hosts. The intended pattern is coexistence: FactLane can sit beside the other memory layers in those workflows rather than trying to replace them.',
      docsAction: 'See the architecture and tested-host boundary →',
    },
    onboarding: {
      eyebrow: 'Start with your agent, not a terminal',
      title: 'Give FactLane to the agent you already use.',
      intro:
        'Ask your agent to read the project, compare it with your current memory stack and explain whether the governed-fact model solves a real problem for you. Installation comes after understanding fit.',
      capabilityTitle: 'What can your agent actually do?',
      capabilityBody:
        'An agent that can read the public repository and docs can explain the project and assess fit. Installing and configuring it requires access to Terminal, files and your MCP-host configuration. Without those permissions, the agent should guide you — not claim it installed anything.',
      steps: [
        {
          title: 'Evaluate fit first',
          body: 'Ask what FactLane adds beside the memory you already have, which facts belong in it, and which information should stay in broader memory or knowledge systems.',
        },
        {
          title: 'Learn the trust model',
          body: 'Understand Candidate, Current, freshness, scope and why remembered information remains below current instructions and live state.',
        },
        {
          title: 'If the agent has access, install read-only',
          body: 'Use the exact v0.1.3 release, the supported local profile and a compatible MCP host. Verify all five tools and a suitable memory_status call before enabling writes.',
        },
        {
          title: 'Enable contribution only after the boundary is clear',
          body: 'If Candidate writes are useful, grant only the delegated-candidate profile. An ordinary agent does not grant itself verifier authority.',
        },
      ],
      promptLabel: 'Prompt for your agent',
      promptTitle: 'Copy this, then adapt it to your workflow.',
      promptBody: `Official FactLane repository:\nhttps://github.com/Habib1001-m/factlane\n\nBefore changing anything on my machine:\n1) Read the project and official docs. Explain what FactLane adds beside the memory systems I already use and whether it fits my workflow.\n2) Give me concrete use cases, and explain Candidate, Current, freshness, scope and the authority boundary in plain language.\n3) If you have Terminal, file and MCP-host configuration access, inspect the prerequisites and install the exact supported v0.1.3 release. Configure it read-only first.\n4) Verify that my host discovers exactly five FactLane tools and that an appropriate memory_status request succeeds.\n5) Before enabling any write capability, explain delegated-candidate versus verifier authority and ask me before changing the write profile.\n\nIf you do not have the required machine or MCP-host access, do not claim you installed or configured anything. Explain and guide me instead.`,
      copyAction: 'Copy prompt',
      copiedAction: 'Prompt copied',
      copyFailedAction: 'Automatic copy failed — select and copy the prompt manually',
      githubAction: 'Open FactLane on GitHub',
      manualAction: 'Prefer manual setup? Open Quick Start',
      promptDirection: 'ltr',
      badge: 'AGENT-NATIVE',
    },
    trust: {
      eyebrow: 'Useful memory without hidden authority',
      title: 'Memory can help without becoming permission.',
      intro:
        'FactLane separates memory eligibility from execution authority. This is the center of the design, not a warning added afterward.',
      cards: [
        {
          title: 'Current reality wins.',
          body: 'A current user instruction, live repository/product state or verified live source outranks memory when they conflict.',
        },
        {
          title: 'Contributors do not verify themselves.',
          body: 'A normal delegated agent can contribute a Candidate when allowed; trusted promotion is a separate operation with revision and identity checks.',
        },
        {
          title: 'Write authority belongs to the launcher boundary.',
          body: 'Seeing memory_store or memory_update does not itself grant permission to use them with privileged semantics.',
        },
      ],
      flow: [
        {label: 'Candidate', body: 'Worth remembering, but not trusted as current yet.'},
        {label: 'Trusted verification', body: 'A separate trusted step checks identity, revision and lifecycle.'},
        {label: 'Current', body: 'Verified, fresh enough and eligible for current-state retrieval.'},
      ],
      docsAction: 'Read Candidate → Current in detail →',
    },
    rigor: {
      eyebrow: 'Quality you can inspect',
      title: 'Free and open source. Production-qualified where we claim support.',
      intro:
        'FactLane v0.1.3 is an official production release for a deliberately bounded local profile. The quality claim is tied to explicit contracts, fail-closed behavior and documented qualification — not adjectives.',
      cards: [
        {
          title: 'Small public surface',
          body: 'Exactly five MCP tools keep the contract focused: search, read, contribute, governed update and status.',
        },
        {
          title: 'Fail-closed defaults',
          body: 'Read-only is the default. Unsupported runtime/provider conditions and unauthorized transitions fail closed instead of silently degrading into broader authority.',
        },
        {
          title: 'Production qualification',
          body: 'The documented profile was exercised for installation, backup/restore, bounded concurrency, crash/restart behavior, configured host startup, production-derived retrieval and SQLite capacity failure.',
        },
        {
          title: 'Inspectable and local',
          body: 'Apache-2.0 source can be inspected, run and adapted. Local SQLite/SQLite-vec storage and supported local Ollama embeddings mean the qualified profile does not require a hosted memory service or external embedding API.',
        },
      ],
      qualification:
        'The boundary is explicit: stdio MCP, Python 3.11+, linked SQLite 3.42.0+, supported local Ollama embeddings and the documented local POSIX storage/recovery contract.',
      architectureAction: 'Read the architecture →',
      securityAction: 'Read the security model →',
    },
    whyNow: {
      eyebrow: 'Why this niche matters now',
      title: 'Persistent agents make memory lifecycle matter.',
      body:
        'When an agent carries context across sessions and acts through tools, a remembered fact can influence work long after the conversation that produced it. That makes freshness, verification and authority boundaries operational concerns, not just retrieval quality.',
      caveat:
        'FactLane stays narrow: it does not solve general agent safety. It governs which reusable facts may count as Current and who is allowed to promote them.',
    },
  },
  ar: {
    fitCheck: {
      eyebrow: 'اختبار ملاءمة سريع',
      title: 'هل هذه مشكلة يحلها FactLane؟',
      intro:
        'تظهر قيمة FactLane عندما تكون هناك حقيقة صغيرة يجب أن تعبر الجلسات، لكنك ما زلت تحتاج معرفة مصدرها وحداثتها وهل أصبحت Current موثوقة فعلًا.',
      items: [
        {
          title: 'وكيلك يعيد تعلّم الحقائق الصغيرة نفسها.',
          body: 'تفضيلات ثابتة وحقائق مشروع وقواعد عمل تعود أهميتها في الجلسة التالية، لكن إعادة المحادثة القديمة كل مرة هدر للسياق.',
        },
        {
          title: 'الحقائق المتذكَّرة قد تصبح قديمة أو مضلِّلة.',
          body: 'تحتاج مصدرًا وحداثة ودورة Candidate → Current صريحة بدل معاملة كل ما تم حفظه كحقيقة موثوقة بالدرجة نفسها.',
        },
        {
          title: 'تريد ذاكرة من دون أن تصبح الذاكرة سلطة.',
          body: 'يجب أن تظل تعليمات المستخدم الحالية وحالة المشروع الحية والمصادر الحية المتحقَّق منها أعلى من أي شيء يتذكره الوكيل.',
        },
      ],
      boundary:
        'إذا كان احتياجك الأساسي أرشيف محادثات كاملًا أو ذاكرة شخصية واسعة أو knowledge graph كبيرًا، فأبقِ ذلك النظام. FactLane هو المسار الأضيق والمحكوم الذي يعمل بجانبه.',
      action: 'شاهد حالات الاستخدام العملية →',
    },
    differentiation: {
      eyebrow: 'ما هو FactLane؟',
      title: 'طبقة حقائق محكومة لوكلاء الذكاء الاصطناعي الذين يحتاجون ذاكرة بين الجلسات.',
      intro:
        'FactLane طبقة ذاكرة MCP مجانية ومفتوحة المصدر ومحلية تعمل خلف AI-agent host متوافق. تحفظ حقائق صغيرة قابلة لإعادة الاستخدام مع scope ومصدر وحداثة؛ يمكن أن تكون المساهمة Candidate من دون أن تصبح Current متحقَّقًا منها، وتظل تعليمات المستخدم الحالية وحالة المشروع الحية والمصادر الحية المتحقَّق منها أعلى من الذاكرة.',
      cards: [
        {
          title: 'ما المشكلة التي يحلها؟',
          body: 'يمكن للوكيل إعادة استخدام تفضيل أو حقيقة مشروع أو قاعدة عمل مفيدة من دون إعادة المحادثة القديمة، ومن دون معاملة ذاكرة قديمة كحقيقة حالية.',
        },
        {
          title: 'كيف يختلف عن الذاكرة المدمجة؟',
          body: 'الذاكرة الواسعة أو الأصلية مناسبة للاستدعاء الغني والاستمرارية. أما FactLane فهو المسار الأضيق للحقائق التي تحتاج scope محددًا ومصدرًا وحداثة وتحقّق Candidate → Current.',
        },
        {
          title: 'ما الذي لا يستبدله؟',
          body: 'لا يستبدل مساعدك أو ذاكرتك الواسعة أو نظام معرفة أو صلاحية التنفيذ. يعمل بجانبها ويحكم فئة صغيرة من الحقائق القابلة لإعادة الاستخدام.',
        },
      ],
      docsAction: 'اقرأ المفاهيم الأساسية →',
    },
    coexistence: {
      eyebrow: 'يكمل، لا يستبدل',
      title: 'أبقِ ذاكرتك الواسعة. وأضف FactLane كمسار محكوم للحقائق.',
      intro:
        'الذاكرة المتكاملة ليست مسألة واحدة لها هندسة صحيحة واحدة. كل طبقة تتفوق في وظيفة مختلفة.',
      lanes: [
        {
          label: 'الذاكرة الواسعة / الأصلية',
          title: 'استدعاء غني واستمرارية',
          body: 'مناسبة للتفضيلات والملخصات والسياق طويل العمر وتجربة الذاكرة الشخصية أو ذاكرة الوكيل الواسعة.',
        },
        {
          label: 'Knowledge graphs / wikis',
          title: 'معرفة مترابطة على نطاق كبير',
          body: 'مناسبة للعلاقات والوثائق والمفاهيم وأجسام المعرفة الكبيرة والمنظمة.',
        },
        {
          label: 'FactLane',
          title: 'مسار محكوم للحقائق',
          body: 'مناسب عندما تحتاج حقيقة صغيرة إلى نطاق محدد ومصدر وحداثة وتحقّق Candidate → Current وحدود صلاحيات صريحة.',
        },
      ],
      note:
        'اختُبر Codex وHermes كـstdio hosts. والنمط المقصود هو التعايش: يمكن لـFactLane أن يعمل بجانب طبقات الذاكرة الأخرى في هذه البيئات بدل محاولة استبدالها.',
      docsAction: 'شاهد المعمارية وحدود الـhosts المختبرة →',
    },
    onboarding: {
      eyebrow: 'ابدأ من وكيلك، لا من سطر الأوامر',
      title: 'أعطِ FactLane للوكيل الذي تستخدمه بالفعل.',
      intro:
        'اطلب من وكيلك قراءة المشروع ومقارنته بطبقات الذاكرة الموجودة لديك وشرح ما إذا كان نموذج الحقائق المحكومة يحل مشكلة حقيقية لك. التثبيت يأتي بعد فهم الملاءمة.',
      capabilityTitle: 'ما الذي يستطيع وكيلك فعله فعلًا؟',
      capabilityBody:
        'الوكيل القادر على قراءة المستودع العام والوثائق يستطيع شرح المشروع وتقييم الملاءمة. أما التثبيت والتهيئة فعليًا فيتطلبان وصولًا إلى Terminal والملفات وإعداد MCP host. من دون هذه الصلاحيات، يجب أن يرشدك فقط — لا أن يدّعي أنه ثبّت شيئًا.',
      steps: [
        {
          title: 'قيّم الملاءمة أولًا',
          body: 'اسأله ماذا يضيف FactLane بجانب الذاكرة التي لديك، وما الحقائق التي تنتمي إليه، وما الذي يجب أن يبقى في ذاكرة أوسع أو نظام معرفة آخر.',
        },
        {
          title: 'افهم نموذج الثقة',
          body: 'تعلّم Candidate وCurrent والحداثة والنطاق ولماذا تظل الذاكرة أدنى من تعليماتك الحالية والحالة الحية.',
        },
        {
          title: 'إن كان قادرًا، ثبّت read-only أولًا',
          body: 'استخدم الإصدار v0.1.3 بالضبط وملف التشغيل المحلي المدعوم وMCP host متوافقًا. تحقّق من الأدوات الخمس وطلب memory_status مناسب قبل أي كتابة.',
        },
        {
          title: 'فعّل المساهمة بعد فهم الحدود',
          body: 'إذا احتجت Candidate writes فامنح فقط delegated-candidate. الوكيل العادي لا يمنح نفسه صلاحية verifier.',
        },
      ],
      promptLabel: 'رسالة جاهزة لوكيلك',
      promptTitle: 'انسخها ثم عدّلها على حسب سير عملك.',
      promptBody: `هذا رابط FactLane الرسمي:\nhttps://github.com/Habib1001-m/factlane\n\nقبل أن تغيّر أي شيء على جهازي:\n1) اقرأ المشروع والوثائق الرسمية. اشرح لي ماذا يضيف FactLane بجانب أنظمة الذاكرة التي أستخدمها وهل يناسب سير عملي.\n2) اقترح حالات استخدام عملية، واشرح ببساطة Candidate وCurrent والحداثة والنطاق وحدود الصلاحيات.\n3) إذا كنت تملك صلاحيات Terminal والملفات وإعداد MCP host، افحص المتطلبات وثبّت الإصدار المدعوم v0.1.3 بالضبط، واضبط الاتصال read-only أولًا.\n4) تحقّق من أن الـhost يكتشف أدوات FactLane الخمس وأن طلب memory_status مناسبًا ينجح.\n5) قبل أي كتابة، اشرح delegated-candidate مقابل verifier authority واطلب موافقتي قبل تغيير write profile.\n\nإذا لم تكن تملك الصلاحيات اللازمة على الجهاز أو إعداد MCP host، لا تدّع أنك ثبّت أو هيّأت شيئًا؛ اشرح وارشدني فقط.`,
      copyAction: 'انسخ الرسالة',
      copiedAction: 'تم نسخ الرسالة',
      copyFailedAction: 'تعذّر النسخ تلقائيًا — حدّد النص وانسخه يدويًا',
      githubAction: 'افتح FactLane على GitHub',
      manualAction: 'تفضّل الإعداد اليدوي؟ افتح Quick Start',
      promptDirection: 'rtl',
      badge: 'AGENT-NATIVE',
    },
    trust: {
      eyebrow: 'ذاكرة نافعة بلا سلطة خفية',
      title: 'الذاكرة تساعد. لكنها لا تصبح صلاحية.',
      intro:
        'يفصل FactLane بين أهلية الذاكرة وصلاحية التنفيذ. هذا جوهر التصميم، وليس تحذيرًا أضفناه بعد اكتمال المنتج.',
      cards: [
        {
          title: 'الحقيقة الحالية تتقدم.',
          body: 'تعليمات المستخدم الحالية أو حالة المشروع/المنتج الحية أو المصدر الحي المتحقَّق منه تتقدم على الذاكرة عند التعارض.',
        },
        {
          title: 'المساهم لا يتحقق من نفسه.',
          body: 'يمكن لوكيل delegated أن يساهم بـCandidate عندما يُسمح له؛ الترقية الموثوقة عملية منفصلة لها فحوص revision وهوية.',
        },
        {
          title: 'صلاحية الكتابة تأتي من launcher boundary.',
          body: 'مجرد ظهور memory_store أو memory_update لا يمنح الوكيل حق استخدامهما بمعاني الصلاحيات الأعلى.',
        },
      ],
      flow: [
        {label: 'Candidate (مرشّح)', body: 'يستحق التذكّر، لكنه ليس حقيقة حالية موثوقة بعد.'},
        {label: 'تحقّق موثوق', body: 'خطوة مستقلة تتحقق من الهوية والـrevision ودورة الحياة.'},
        {label: 'Current (حالي ومتحقَّق منه)', body: 'متحقَّق منه، حديث بما يكفي، ومؤهل للاسترجاع الحالي.'},
      ],
      docsAction: 'اقرأ Candidate → Current بالتفصيل →',
    },
    rigor: {
      eyebrow: 'جودة يمكنك فحصها',
      title: 'مجاني ومفتوح المصدر. ومؤهل إنتاجيًا ضمن نطاق الدعم المعلن.',
      intro:
        'FactLane v0.1.3 إصدار إنتاج رسمي لملف محلي محدد عمدًا. ادعاء الجودة مربوط بعقود صريحة وفشل مغلق وتأهيل موثّق — لا بصفات تسويقية.',
      cards: [
        {
          title: 'سطح عام صغير',
          body: 'خمس أدوات MCP فقط تغطي البحث والقراءة والمساهمة والتحديث المحكوم والحالة.',
        },
        {
          title: 'Fail-closed افتراضيًا',
          body: 'الوضع الافتراضي read-only. حالات runtime/provider غير المدعومة والانتقالات غير المصرح بها تفشل مغلقة بدل توسيع الصلاحيات بصمت.',
        },
        {
          title: 'تأهيل إنتاجي موثّق',
          body: 'الملف الموثق اختُبر للتثبيت وbackup/restore وbounded concurrency وcrash/restart وconfigured host startup وproduction-derived retrieval وفشل سعة SQLite.',
        },
        {
          title: 'محلي وقابل للفحص',
          body: 'يتيح ترخيص Apache-2.0 فحص الكود وتشغيله وتكييفه. ومع تخزين SQLite/SQLite-vec محلي وOllama embeddings محلية ومدعومة، لا يحتاج ملف التشغيل المؤهل إلى خدمة ذاكرة مستضافة أو API خارجي للـembeddings.',
        },
      ],
      qualification:
        'الحدود صريحة: stdio MCP وPython 3.11+ وSQLite 3.42.0+ المرتبط بـPython وOllama embeddings محلية ومدعومة وعقد POSIX storage/recovery المحلي الموثق.',
      architectureAction: 'اقرأ المعمارية →',
      securityAction: 'اقرأ نموذج الأمان →',
    },
    whyNow: {
      eyebrow: 'لماذا يهم هذا المجال الآن؟',
      title: 'استمرار الوكيل عبر الجلسات يجعل دورة حياة الذاكرة مهمة.',
      body:
        'عندما يحمل الوكيل سياقًا بين الجلسات ويستخدم أدوات، يمكن لحقيقة متذكَّرة أن تؤثر في عمل لاحق بعد زمن من المحادثة التي أنتجتها. عندها تصبح الحداثة والتحقّق وحدود الصلاحيات مسائل تشغيلية، لا مجرد جودة استرجاع.',
      caveat:
        'يبقى نطاق FactLane أضيق: لا يدّعي حل أمان الوكلاء عمومًا؛ بل يحكم أي الحقائق القابلة لإعادة الاستخدام يجوز اعتبارها Current، ومن يملك صلاحية ترقيتها.',
    },
  },
};
