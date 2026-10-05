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
      title: 'Memory your AI assistant can actually reuse',
      description:
        'FactLane helps compatible AI assistants remember useful facts across sessions without turning old memory into automatic authority.',
      structuredDataDescription:
        'Local-first governed memory for compatible AI assistants, with bounded facts, scope, provenance, freshness and trusted verification.',
    },
    hero: {
      kicker: 'Useful memory across sessions for compatible AI assistants',
      title: 'Let your assistant remember the useful things.',
      titleMuted: ' Not the whole conversation.',
      lead:
        'FactLane helps compatible AI assistants remember your preferences, projects and recurring facts across sessions — without treating every old memory as permanent truth.',
      primaryCta: 'See what it can remember',
      secondaryCta: "Why memory won't overrule you",
      proofsLabel: 'FactLane at a glance',
      proofs: [
        {strong: 'Stays local', rest: ' in the supported setup'},
        {strong: 'Facts', rest: ', not transcripts'},
        {strong: 'Current instructions', rest: ' still win'},
      ],
      developerLabel: 'Developer?',
      developerLink: 'Go straight to the technical setup →',
      windowTitle: 'FactLane · reusable memory',
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
      eyebrow: 'Start with the outcome',
      title: 'What could FactLane remember for you?',
      intro:
        'You do not need to know MCP, embeddings, vector search or SQLite to understand the point: keep the useful fact, without replaying the whole chat.',
      items: [
        {
          eyebrow: 'Your preferences',
          title: 'Stop repeating how you like things done.',
          quote: '“Keep answers concise, explain in Arabic, and preserve English technical terms.”',
          note: 'A useful preference can be reused in later sessions instead of being re-explained.',
        },
        {
          eyebrow: 'Your projects',
          title: 'Carry important project facts forward.',
          quote: '“Remember the product goal, the current release, and the decisions we already made.”',
          note: 'Your next session can start with useful established facts rather than a blank slate.',
        },
        {
          eyebrow: 'Your workflow',
          title: 'Keep recurring rules available.',
          quote: '“Production changes need approval. Local tests can run automatically.”',
          note: 'Workflow facts stay available without replaying an old conversation every time.',
        },
        {
          eyebrow: 'Across assistants',
          title: 'Reuse facts without sharing whole chats.',
          quote: '“Let my coding assistant and another AI use the same approved project facts.”',
          note: 'FactLane shares bounded facts, not entire transcripts or conversational context.',
        },
      ],
      link: 'See more everyday examples →',
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
      eyebrow: 'Evidence before adjectives',
      title: 'Qualified where we can prove it. Explicit where we cannot.',
      intro:
        'FactLane v0.1.3 is the first official production release for a documented local profile — not a universal deployment claim.',
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
      eyebrow: 'Share facts. Not context.',
      title: 'Give your assistants memory without giving old memory the final word.',
      fitCta: 'See if FactLane fits',
      setupCta: 'Start the technical setup',
      githubCta: 'View on GitHub ↗',
    },
  },
  ar: {
    meta: {
      title: 'ذاكرة يستطيع مساعدك إعادة استخدامها فعلًا',
      description:
        'يساعد FactLane مساعدات الذكاء الاصطناعي المتوافقة على تذكّر الحقائق المفيدة بين الجلسات من دون تحويل الذاكرة القديمة إلى سلطة تلقائية.',
      structuredDataDescription:
        'ذاكرة محلية بضوابط واضحة لمساعدات الذكاء الاصطناعي المتوافقة، مع حقائق محدودة النطاق، ومصدر واضح، وحداثة، وتحقّق موثوق.',
    },
    hero: {
      kicker: 'ذاكرة مفيدة بين الجلسات لمساعدات الذكاء الاصطناعي المتوافقة',
      title: 'دع مساعدك يتذكّر ما يفيدك.',
      titleMuted: ' لا المحادثة كاملةً.',
      lead:
        'يساعد FactLane مساعدات الذكاء الاصطناعي المتوافقة على تذكّر تفضيلاتك ومشروعاتك والحقائق المتكررة بين الجلسات — من دون اعتبار كل ذاكرة قديمة حقيقة دائمة.',
      primaryCta: 'شاهد ما يمكنه تذكّره',
      secondaryCta: 'لماذا لا تتجاوز الذاكرة تعليماتك؟',
      proofsLabel: 'FactLane باختصار',
      proofs: [
        {strong: 'يبقى محليًا', rest: ' ضمن الإعداد المدعوم'},
        {strong: 'حقائق', rest: ' لا نصوص محادثات'},
        {strong: 'تعليماتك الحالية', rest: ' لها الأولوية'},
      ],
      developerLabel: 'مطور؟',
      developerLink: 'انتقل مباشرةً إلى الإعداد التقني',
      windowTitle: 'FactLane · ذاكرة قابلة لإعادة الاستخدام',
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
      eyebrow: 'ابدأ بالنتيجة',
      title: 'ما الذي يمكن أن يتذكّره FactLane لك؟',
      intro:
        'لا تحتاج إلى معرفة MCP أو embeddings أو vector search أو SQLite لفهم الفكرة: احتفظ بالحقيقة المفيدة بدل إعادة المحادثة كاملةً.',
      items: [
        {
          eyebrow: 'تفضيلاتك',
          title: 'لا تكرر في كل مرة كيف تفضّل العمل.',
          quote: '«اجعل الإجابات مختصرة، واشرح بالعربية، وأبقِ المصطلحات التقنية بالإنجليزية.»',
          note: 'يمكن إعادة استخدام التفضيل المفيد في جلسات لاحقة بدل شرحه من جديد.',
        },
        {
          eyebrow: 'مشروعاتك',
          title: 'احمل حقائق المشروع المهمة إلى الجلسة التالية.',
          quote: '«تذكّر هدف المنتج، والإصدار الحالي، والقرارات التي اتخذناها بالفعل.»',
          note: 'تبدأ الجلسة التالية من حقائق مستقرة ومفيدة بدل صفحة فارغة.',
        },
        {
          eyebrow: 'سير العمل',
          title: 'اجعل القواعد المتكررة متاحة دائمًا.',
          quote: '«تغييرات بيئة الإنتاج تحتاج موافقة. الاختبارات المحلية يمكن تشغيلها تلقائيًا.»',
          note: 'تبقى قواعد العمل متاحة من دون إعادة تشغيل محادثة قديمة في كل مرة.',
        },
        {
          eyebrow: 'بين المساعدات',
          title: 'أعد استخدام الحقائق من دون مشاركة المحادثات كاملةً.',
          quote: '«اجعل مساعد البرمجة ومساعد ذكاء اصطناعي آخر يستخدمان حقائق المشروع المعتمدة نفسها.»',
          note: 'يشارك FactLane حقائق محدودة، لا النص الكامل للمحادثات ولا سياقها الكامل.',
        },
      ],
      link: 'شاهد أمثلة يومية أكثر',
    },
    everydayFit: {
      eyebrow: 'ما معنى استخدامه فعليًا؟',
      title: 'FactLane ليس روبوت محادثة جديدًا. إنه طبقة الذاكرة خلف مساعدك.',
      intro:
        'بعد توصيل مساعد متوافق، يمكن أن يبقى الاستخدام اليومي بسيطًا: استمر في استخدام المساعد الذي تعرفه، بينما تبقى الحقائق المفيدة المعتمدة متاحة بين الجلسات.',
      cards: [
        {
          title: 'تستمر في استخدام مساعدك.',
          body: 'FactLane لا يستبدل مساعد المحادثة أو البرمجة. بل يمنح host متوافقًا مكانًا محليًا لحفظ الحقائق المفيدة.',
        },
        {
          title: 'الحقيقة المفيدة تبقى بعد انتهاء الجلسة.',
          body: 'يمكن إعادة استخدام التفضيلات وحقائق المشروع والقواعد المتكررة لاحقًا من دون إعادة المحادثة القديمة كاملةً.',
        },
        {
          title: 'الإعداد الحالي ما زال تقنيًا.',
          body: 'يعمل FactLane v0.1.3 محليًا من خلال MCP host متوافق، ولذلك يجب إعداد هذا الاتصال أولًا، حتى لو كان الاستخدام اليومي بعد ذلك أبسط بكثير.',
        },
      ],
      beginnerCta: 'أنا جديد — اشرحها ببساطة',
      setupCta: 'أنا جاهز للإعداد التقني',
      faqCta: 'ما زلت غير متأكد؟ اقرأ الأسئلة الشائعة',
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
      eyebrow: 'الدليل قبل الصفات',
      title: 'نقول «مؤهل» حيث نستطيع إثباته، ونصرّح بالحدود حيث لا نستطيع.',
      intro:
        'FactLane v0.1.3 هو أول إصدار إنتاج رسمي ضمن ملف تشغيل محلي موثَّق — وليس ادعاءً بأنه مناسب لكل بيئة نشر.',
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
      title: 'خمس أدوات MCP مركزة.',
      intro:
        'تبقى الواجهة العامة صغيرة عمدًا. ظهور الأداة لا يمنح صلاحية الكتابة في حد ذاته.',
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
      eyebrow: 'معمارية Local-first',
      title: 'يعمل بجوار مساعداتك، لا كسحابة ذاكرة بعيدة.',
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
      eyebrow: 'شارك الحقائق، لا السياق.',
      title: 'امنح مساعداتك ذاكرة من دون أن تمنح الذاكرة القديمة الكلمة الأخيرة.',
      fitCta: 'اعرف هل FactLane مناسب لك',
      setupCta: 'ابدأ الإعداد التقني',
      githubCta: 'اعرض المشروع على GitHub',
    },
  },
};
