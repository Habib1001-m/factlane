import React from 'react';
import type {ReactNode} from 'react';
import clsx from 'clsx';
import Head from '@docusaurus/Head';
import Link from '@docusaurus/Link';
import useDocusaurusContext from '@docusaurus/useDocusaurusContext';
import Layout from '@theme/Layout';
import Heading from '@theme/Heading';
import styles from './index.module.css';

const useCases = [
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
];

const tools = [
  ['memory_search', 'Find eligible facts inside one exact scope.'],
  ['memory_get', 'Inspect one memory and its provenance or revision details.'],
  ['memory_store', 'Contribute one bounded fact when the launcher allows it.'],
  ['memory_update', 'Reverify or replace under trusted revision rules.'],
  ['memory_status', 'Inspect bounded storage and embedding-profile health.'],
];

function Section({
  id,
  eyebrow,
  title,
  intro,
  children,
  soft = false,
}: {
  id?: string;
  eyebrow?: string;
  title: string;
  intro?: string;
  children: ReactNode;
  soft?: boolean;
}) {
  return (
    <section id={id} className={clsx(styles.section, soft && styles.sectionSoft)}>
      <div className={styles.container}>
        <div className={styles.sectionHeading}>
          {eyebrow && <div className={styles.eyebrow}>{eyebrow}</div>}
          <Heading as="h2">{title}</Heading>
          {intro && <p>{intro}</p>}
        </div>
        {children}
      </div>
    </section>
  );
}

function Hero() {
  return (
    <header className={styles.hero}>
      <div className={clsx(styles.container, styles.heroGrid)}>
        <div className={styles.heroCopy}>
          <div className={styles.heroKicker}>
            <span className={styles.statusDot} aria-hidden="true" />
            Useful memory across sessions for compatible AI assistants
          </div>
          <Heading as="h1">
            Let your assistant remember the useful things.
            <span> Not the whole conversation.</span>
          </Heading>
          <p className={styles.heroLead}>
            FactLane helps compatible AI assistants remember your preferences, projects and recurring
            facts across sessions — without treating every old memory as permanent truth.
          </p>
          <div className={styles.heroActions}>
            <a className="button button--primary button--lg" href="#use-cases">
              See what it can remember
            </a>
            <a className="button button--secondary button--lg" href="#how-it-works">
              Why memory won&apos;t overrule you
            </a>
          </div>
          <ul className={styles.heroProofs} aria-label="FactLane at a glance">
            <li><strong>Stays local</strong> in the supported setup</li>
            <li><strong>Facts</strong>, not transcripts</li>
            <li><strong>Current instructions</strong> still win</li>
          </ul>
          <p className={styles.technicalQualifier}>
            <strong>Developer?</strong>{' '}
            <Link to="/docs/QUICKSTART">Go straight to the technical setup →</Link>
          </p>
        </div>

        <figure className={styles.heroVisual}>
          <div className={styles.memoryWindow}>
            <div className={styles.windowTop}>
              <div className={styles.windowDots} aria-hidden="true">
                <span />
                <span />
                <span />
              </div>
              <span>FactLane · reusable memory</span>
              <span className={styles.localBadge}>LOCAL</span>
            </div>
            <div className={styles.memoryBody}>
              <div className={styles.memoryPrompt}>
                <span>You</span>
                Remember that I prefer concise answers in Arabic, but keep technical terms in
                English.
              </div>
              <div className={styles.memoryRecord}>
                <div className={styles.recordTop}>
                  <span>After trusted verification · Preference</span>
                  <span className={styles.currentBadge}>CURRENT</span>
                </div>
                <strong>Concise Arabic explanations + English technical terms</strong>
                <div className={styles.recordMeta}>
                  <span>scope · global user</span>
                  <span>source · user instruction</span>
                </div>
              </div>
              <div className={styles.memoryLater}>
                <span>Next week</span>
                <p>
                  Your assistant can reuse the approved preference without rebuilding the
                  context from scratch.
                </p>
              </div>
            </div>
          </div>
          <figcaption className={styles.heroCaption}>Share facts. Not context.</figcaption>
        </figure>
      </div>
    </header>
  );
}

function UseCases() {
  return (
    <Section
      id="use-cases"
      eyebrow="Start with the outcome"
      title="What could FactLane remember for you?"
      intro="You do not need to know MCP, embeddings, vector search or SQLite to understand the point: keep the useful fact, without replaying the whole chat.">
      <div className={styles.useCaseGrid}>
        {useCases.map((item) => (
          <article key={item.eyebrow} className={styles.useCaseCard}>
            <div className={styles.cardEyebrow}>{item.eyebrow}</div>
            <Heading as="h3">{item.title}</Heading>
            <blockquote>{item.quote}</blockquote>
            <p>{item.note}</p>
          </article>
        ))}
      </div>
      <div className={styles.sectionCta}>
        <Link to="/docs/USE_CASES">See more everyday examples →</Link>
      </div>
    </Section>
  );
}

function EverydayFit() {
  return (
    <Section
      id="everyday-fit"
      eyebrow="What using it actually means"
      title="FactLane is not another chatbot. It is the memory layer behind one."
      intro="Once a compatible assistant is connected, your day-to-day experience can stay simple: keep talking to the assistant you already use while useful approved facts remain available between sessions."
      soft>
      <div className={styles.fitGrid}>
        <article className={styles.fitCard}>
          <span className={styles.fitNumber}>01</span>
          <Heading as="h3">You keep using your assistant.</Heading>
          <p>
            FactLane does not replace the chat or coding assistant. It gives a compatible host a
            local place to keep useful facts.
          </p>
        </article>
        <article className={styles.fitCard}>
          <span className={styles.fitNumber}>02</span>
          <Heading as="h3">The useful fact survives the session.</Heading>
          <p>
            Preferences, project facts and recurring rules can be reused later without replaying
            the entire old conversation.
          </p>
        </article>
        <article className={styles.fitCard}>
          <span className={styles.fitNumber}>03</span>
          <Heading as="h3">Setup today is still technical.</Heading>
          <p>
            FactLane v0.1.3 runs locally through a compatible MCP host. Someone must configure that
            connection, even though normal use afterward does not require thinking about databases
            or vector search.
          </p>
        </article>
      </div>
      <div className={styles.fitActions}>
        <Link className="button button--primary" to="/docs/">
          I&apos;m new — explain it simply
        </Link>
        <Link className="button button--secondary" to="/docs/QUICKSTART">
          I&apos;m ready for technical setup
        </Link>
        <Link className={styles.fitTextLink} to="/docs/FAQ">
          Not sure yet? Read the FAQ →
        </Link>
      </div>
    </Section>
  );
}

function WhyGoverned() {
  return (
    <Section
      id="why-governed"
      eyebrow="Why not just remember everything?"
      title="Remembering is useful. Blindly trusting old memory is not."
      intro="Preferences change. Projects move forward. A suggestion can be wrong. FactLane keeps memory useful without letting it silently overrule what is true now."
      soft>
      <div className={styles.trustGrid}>
        <article className={styles.trustCard}>
          <span className={styles.trustNumber}>01</span>
          <Heading as="h3">Old information can become stale.</Heading>
          <p>“The release is v0.1.2” might have been true last month and wrong today.</p>
        </article>
        <article className={styles.trustCard}>
          <span className={styles.trustNumber}>02</span>
          <Heading as="h3">A new suggestion is not automatically trusted.</Heading>
          <p>
            An assistant can propose something worth remembering without being allowed to
            declare it current truth.
          </p>
        </article>
        <article className={styles.trustCard}>
          <span className={styles.trustNumber}>03</span>
          <Heading as="h3">What is true now should win.</Heading>
          <p>
            Current user instructions, live project state and verified live sources outrank
            remembered facts.
          </p>
        </article>
      </div>
    </Section>
  );
}

function Lifecycle() {
  const steps = [
    ['Something useful appears', 'Your assistant proposes a fact.', 'Example: “This project deploys only after owner approval.”'],
    ['Candidate', 'Worth remembering, not trusted yet.', 'The assistant cannot promote its own contribution just by claiming authority.'],
    ['Trusted verification', 'A separate trusted step checks it.', 'Revision and identity checks prevent stale or mismatched promotion.'],
    ['Current', 'Verified and eligible to use now.', 'Normal current-state retrieval excludes unverified Candidates.'],
  ];

  return (
    <Section
      id="how-it-works"
      eyebrow="A simple mental model"
      title="A fact can be worth remembering before it is trusted."
      intro="FactLane separates “save this for later” from “this is current and verified.” That is the core idea behind Candidate → Current.">
      <div className={styles.lifecycle}>
        {steps.map(([label, title, copy], index) => (
          <React.Fragment key={label}>
            <article className={styles.lifecycleStep}>
              <div className={styles.lifecycleIcon}>{index + 1}</div>
              <div>
                <span>{label}</span>
                <strong>{title}</strong>
                <p>{copy}</p>
              </div>
            </article>
            {index < steps.length - 1 && (
              <div className={styles.lifecycleConnector} aria-hidden="true" />
            )}
          </React.Fragment>
        ))}
      </div>
      <div className={styles.sectionCta}>
        <Link to="/docs/CORE_CONCEPTS">Understand Candidate → Current →</Link>
      </div>
    </Section>
  );
}

function Authority() {
  return (
    <Section
      id="trust-model"
      eyebrow="The important boundary"
      title="Memory supports the decision. It does not own the decision."
      intro="FactLane remembers what was established, but current reality still has the final say."
      soft>
      <div className={styles.authorityLayout}>
        <div className={styles.authorityStack}>
          <div className={styles.authorityTop}>
            <span>Higher authority</span>
            <strong>What is true now</strong>
            <ul>
              <li>Current user instruction</li>
              <li>Live repository or product state</li>
              <li>Verified live source</li>
            </ul>
          </div>
          <div className={styles.authorityArrow}>outranks ↓</div>
          <div className={styles.authorityMemory}>
            <span>Supporting evidence</span>
            <strong>FactLane memory</strong>
            <p>Durable, scoped and provenance-aware — but still memory.</p>
          </div>
        </div>
        <div className={styles.authorityCopy}>
          <Heading as="h3">Why this matters in real life</Heading>
          <p>
            Suppose FactLane remembers that you prefer short answers. That is useful tomorrow.
            But if you say “go deep on this one,” your current instruction wins.
          </p>
          <p>
            The same principle applies to project versions, deployment rules, ownership,
            environment state and other facts that can change over time.
          </p>
        </div>
      </div>
    </Section>
  );
}

function Tools() {
  return (
    <Section
      id="tools"
      eyebrow="For developers"
      title="Five focused MCP tools."
      intro="The public surface stays intentionally small. Tool visibility does not itself grant write authority."
      soft>
      <div className={styles.toolList}>
        {tools.map(([name, description]) => (
          <div key={name} className={styles.toolRow}>
            <code>{name}</code>
            <span>{description}</span>
          </div>
        ))}
      </div>
      <div className={styles.sectionCta}>
        <Link to="/docs/TOOLS">Read the tool guide →</Link>
      </div>
    </Section>
  );
}

function Architecture() {
  return (
    <Section
      id="architecture"
      eyebrow="Local-first architecture"
      title="Runs beside your assistants, not as a remote memory cloud."
      intro="The current supported profile is a command-launched stdio MCP server with local embeddings and local SQLite/SQLite-vec storage.">
      <div className={styles.architectureGrid}>
        <div className={styles.architectureFlow}>
          <div>MCP host / trusted launcher</div>
          <span>↓</span>
          <div>Host + write-context bindings</div>
          <span>↓</span>
          <div>FactLane · five MCP tools</div>
          <span>↓</span>
          <div>Scope · identity · authority checks</div>
          <span>↓</span>
          <div className={styles.architectureFork}>
            <div>Local Ollama</div>
            <div>SQLite / sqlite-vec</div>
          </div>
        </div>
        <div className={styles.architectureNotes}>
          <div>
            <strong>stdio only</strong>
            <p>No HTTP, SSE or Streamable HTTP server transport in v0.1.3.</p>
          </div>
          <div>
            <strong>local embeddings</strong>
            <p>The shipped provider uses Ollama over loopback HTTP.</p>
          </div>
          <div>
            <strong>authorization belongs to FactLane</strong>
            <p>The storage backend does not decide who is allowed to promote memory.</p>
          </div>
          <Link to="/docs/ARCHITECTURE">Read the architecture →</Link>
        </div>
      </div>
    </Section>
  );
}

function QuickStart() {
  const command = [
    'git clone --branch v0.1.3 --depth 1 \\',
    '  https://github.com/Habib1001-m/factlane.git',
    'cd factlane',
    'uv sync --frozen',
    "uv run python -c 'import sqlite3; print(sqlite3.sqlite_version)'",
    'ollama pull embeddinggemma:300m',
    'uv run factlane --help-tools',
  ].join('\n');

  return (
    <Section
      id="quick-start"
      eyebrow="Ready to try it?"
      title="Start local. Keep the first run boring."
      intro="Use the exact v0.1.3 release, verify the linked SQLite runtime, install a supported local embedding model, then connect your MCP host."
      soft>
      <div className={styles.quickStart}>
        <div className={styles.codePanel}>
          <div className={styles.codeHeader}>
            <span>terminal</span>
            <span>v0.1.3</span>
          </div>
          <pre><code>{command}</code></pre>
        </div>
        <div className={styles.quickStartCopy}>
          <Heading as="h3">New to the technical setup?</Heading>
          <p>
            The Quick Start explains the prerequisites one step at a time, including what
            launches FactLane and why “five tools discovered” does not automatically mean
            “writes are allowed.”
          </p>
          <Link className="button button--primary button--lg" to="/docs/QUICKSTART">
            Follow the Quick Start
          </Link>
        </div>
      </div>
    </Section>
  );
}

function Boundaries() {
  return (
    <Section
      id="qualified-profile"
      eyebrow="Evidence before adjectives"
      title="Qualified where we can prove it. Explicit where we cannot."
      intro="FactLane v0.1.3 is the first official production release for a documented local profile — not a universal deployment claim.">
      <div className={styles.boundaryGrid}>
        <div className={styles.boundaryCard}>
          <span className={styles.goodLabel}>Supported profile</span>
          <ul>
            <li>Python 3.11+</li>
            <li>linked SQLite 3.42.0+</li>
            <li>command-launched stdio MCP</li>
            <li>supported local Ollama embeddings</li>
            <li>documented local POSIX storage/recovery contract</li>
          </ul>
        </div>
        <div className={styles.boundaryCard}>
          <span className={styles.limitLabel}>Current limits</span>
          <ul>
            <li>no remote embedding endpoint fallback</li>
            <li>no HTTP/SSE/Streamable HTTP server transport</li>
            <li>not a transcript archive or general document crawler</li>
            <li>facts are bounded to 2,000 UTF-8 bytes</li>
            <li>language/semantic quality remains workload-specific</li>
          </ul>
        </div>
      </div>
      <div className={styles.sectionCta}>
        <Link to="/docs/ENVIRONMENT">Read environment and compatibility →</Link>
      </div>
    </Section>
  );
}

function FinalCta() {
  return (
    <section className={styles.finalCta}>
      <div className={styles.container}>
        <div className={styles.finalCtaPanel}>
          <div>
            <div className={styles.eyebrow}>Share facts. Not context.</div>
            <Heading as="h2">
              Give your assistants memory without giving old memory the final word.
            </Heading>
          </div>
          <div className={styles.finalCtaActions}>
            <Link className="button button--primary button--lg" to="/docs/FAQ">
              See if FactLane fits
            </Link>
            <Link className="button button--secondary button--lg" to="/docs/QUICKSTART">
              Start the technical setup
            </Link>
            <Link className={styles.textLink} href="https://github.com/Habib1001-m/factlane">
              View on GitHub ↗
            </Link>
          </div>
        </div>
      </div>
    </section>
  );
}

export default function Home(): ReactNode {
  const {siteConfig} = useDocusaurusContext();
  const structuredData = {
    '@context': 'https://schema.org',
    '@type': 'SoftwareSourceCode',
    name: 'FactLane',
    description:
      'Local-first governed memory for compatible AI assistants, with bounded facts, scope, provenance, freshness and trusted verification.',
    codeRepository: 'https://github.com/Habib1001-m/factlane',
    programmingLanguage: 'Python',
    runtimePlatform: 'Python 3.11+',
    softwareVersion: '0.1.3',
    license: 'https://www.apache.org/licenses/LICENSE-2.0',
    url: siteConfig.url,
  };

  return (
    <Layout
      title="Memory your AI assistant can actually reuse"
      description="FactLane helps compatible AI assistants remember useful facts across sessions without turning old memory into automatic authority.">
      <Head>
        <script type="application/ld+json">{JSON.stringify(structuredData)}</script>
      </Head>
      <main>
        <Hero />
        <UseCases />
        <EverydayFit />
        <WhyGoverned />
        <Lifecycle />
        <Authority />
        <QuickStart />
        <Boundaries />
        <Tools />
        <Architecture />
        <FinalCta />
      </main>
    </Layout>
  );
}
