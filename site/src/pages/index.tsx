import React from 'react';
import type {ReactNode} from 'react';
import clsx from 'clsx';
import Head from '@docusaurus/Head';
import Link from '@docusaurus/Link';
import useDocusaurusContext from '@docusaurus/useDocusaurusContext';
import Layout from '@theme/Layout';
import Heading from '@theme/Heading';
import {homeCopy, type HomeCopy} from '../content/homeCopy';
import styles from './index.module.css';

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

function Hero({copy}: {copy: HomeCopy}) {
  return (
    <header className={styles.hero}>
      <div className={clsx(styles.container, styles.heroGrid)}>
        <div className={styles.heroCopy}>
          <div className={styles.heroKicker}>
            <span className={styles.statusDot} aria-hidden="true" />
            {copy.hero.kicker}
          </div>
          <Heading as="h1">
            {copy.hero.title}
            <span>{copy.hero.titleMuted}</span>
          </Heading>
          <p className={styles.heroLead}>{copy.hero.lead}</p>
          <div className={styles.heroActions}>
            <a className="button button--primary button--lg" href="#use-cases">
              {copy.hero.primaryCta}
            </a>
            <a className="button button--secondary button--lg" href="#how-it-works">
              {copy.hero.secondaryCta}
            </a>
          </div>
          <ul className={styles.heroProofs} aria-label={copy.hero.proofsLabel}>
            {copy.hero.proofs.map((proof) => (
              <li key={proof.strong}>
                <strong>{proof.strong}</strong>
                {proof.rest}
              </li>
            ))}
          </ul>
          <p className={styles.technicalQualifier}>
            <strong>{copy.hero.developerLabel}</strong>{' '}
            <Link to="/docs/QUICKSTART">{copy.hero.developerLink}</Link>
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
              <span><bdi dir="auto">{copy.hero.windowTitle}</bdi></span>
              <span className={styles.localBadge}>{copy.hero.localBadge}</span>
            </div>
            <div className={styles.memoryBody}>
              <div className={styles.memoryPrompt}>
                <span>{copy.hero.promptLabel}</span>
                {copy.hero.prompt}
              </div>
              <div className={styles.memoryRecord}>
                <div className={styles.recordTop}>
                  <span>{copy.hero.verifiedLabel}</span>
                  <span className={styles.currentBadge}>{copy.hero.currentBadge}</span>
                </div>
                <strong>{copy.hero.record}</strong>
                <div className={styles.recordMeta}>
                  <span>{copy.hero.scope}</span>
                  <span>{copy.hero.source}</span>
                </div>
              </div>
              <div className={styles.memoryLater}>
                <span>{copy.hero.laterLabel}</span>
                <p>{copy.hero.later}</p>
              </div>
            </div>
          </div>
          <figcaption className={styles.heroCaption}>{copy.hero.caption}</figcaption>
        </figure>
      </div>
    </header>
  );
}

function UseCases({copy}: {copy: HomeCopy}) {
  return (
    <Section
      id="use-cases"
      eyebrow={copy.useCases.eyebrow}
      title={copy.useCases.title}
      intro={copy.useCases.intro}>
      <div className={styles.useCaseGrid}>
        {copy.useCases.items.map((item) => (
          <article key={item.eyebrow} className={styles.useCaseCard}>
            <div className={styles.cardEyebrow}>{item.eyebrow}</div>
            <Heading as="h3">{item.title}</Heading>
            <blockquote>{item.quote}</blockquote>
            <p>{item.note}</p>
          </article>
        ))}
      </div>
      <div className={styles.sectionCta}>
        <Link to="/docs/USE_CASES">{copy.useCases.link}</Link>
      </div>
    </Section>
  );
}

function EverydayFit({copy}: {copy: HomeCopy}) {
  return (
    <Section
      id="everyday-fit"
      eyebrow={copy.everydayFit.eyebrow}
      title={copy.everydayFit.title}
      intro={copy.everydayFit.intro}
      soft>
      <div className={styles.fitGrid}>
        {copy.everydayFit.cards.map((card, index) => (
          <article key={card.title} className={styles.fitCard}>
            <span className={styles.fitNumber}>0{index + 1}</span>
            <Heading as="h3">{card.title}</Heading>
            <p>{card.body}</p>
          </article>
        ))}
      </div>
      <div className={styles.fitActions}>
        <Link className="button button--primary" to="/docs/">
          {copy.everydayFit.beginnerCta}
        </Link>
        <Link className="button button--secondary" to="/docs/QUICKSTART">
          {copy.everydayFit.setupCta}
        </Link>
        <Link className={styles.fitTextLink} to="/docs/FAQ">
          {copy.everydayFit.faqCta}
        </Link>
      </div>
    </Section>
  );
}

function WhyGoverned({copy}: {copy: HomeCopy}) {
  return (
    <Section
      id="why-governed"
      eyebrow={copy.whyGoverned.eyebrow}
      title={copy.whyGoverned.title}
      intro={copy.whyGoverned.intro}
      soft>
      <div className={styles.trustGrid}>
        {copy.whyGoverned.cards.map((card, index) => (
          <article key={card.title} className={styles.trustCard}>
            <span className={styles.trustNumber}>0{index + 1}</span>
            <Heading as="h3">{card.title}</Heading>
            <p>{card.body}</p>
          </article>
        ))}
      </div>
    </Section>
  );
}

function Lifecycle({copy}: {copy: HomeCopy}) {
  return (
    <Section
      id="how-it-works"
      eyebrow={copy.lifecycle.eyebrow}
      title={copy.lifecycle.title}
      intro={copy.lifecycle.intro}>
      <div className={styles.lifecycle}>
        {copy.lifecycle.steps.map((step, index) => (
          <React.Fragment key={step.label}>
            <article className={styles.lifecycleStep}>
              <div className={styles.lifecycleIcon}>{index + 1}</div>
              <div>
                <span><bdi dir="auto">{step.label}</bdi></span>
                <strong>{step.title}</strong>
                <p>{step.body}</p>
              </div>
            </article>
            {index < copy.lifecycle.steps.length - 1 && (
              <div className={styles.lifecycleConnector} aria-hidden="true" />
            )}
          </React.Fragment>
        ))}
      </div>
      <div className={styles.sectionCta}>
        <Link to="/docs/CORE_CONCEPTS">{copy.lifecycle.link}</Link>
      </div>
    </Section>
  );
}

function Authority({copy}: {copy: HomeCopy}) {
  return (
    <Section
      id="trust-model"
      eyebrow={copy.authority.eyebrow}
      title={copy.authority.title}
      intro={copy.authority.intro}
      soft>
      <div className={styles.authorityLayout}>
        <div className={styles.authorityStack}>
          <div className={styles.authorityTop}>
            <span>{copy.authority.higherAuthority}</span>
            <strong>{copy.authority.trueNow}</strong>
            <ul>
              {copy.authority.trueNowItems.map((item) => <li key={item}>{item}</li>)}
            </ul>
          </div>
          <div className={styles.authorityArrow}>{copy.authority.outranks}</div>
          <div className={styles.authorityMemory}>
            <span>{copy.authority.supportingEvidence}</span>
            <strong>{copy.authority.factLaneMemory}</strong>
            <p>{copy.authority.memoryBody}</p>
          </div>
        </div>
        <div className={styles.authorityCopy}>
          <Heading as="h3">{copy.authority.realLifeTitle}</Heading>
          {copy.authority.realLifeParagraphs.map((paragraph) => (
            <p key={paragraph}>{paragraph}</p>
          ))}
        </div>
      </div>
    </Section>
  );
}

function QuickStart({copy}: {copy: HomeCopy}) {
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
      eyebrow={copy.quickStart.eyebrow}
      title={copy.quickStart.title}
      intro={copy.quickStart.intro}
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
          <Heading as="h3">{copy.quickStart.panelTitle}</Heading>
          <p>{copy.quickStart.body}</p>
          <Link className="button button--primary button--lg" to="/docs/QUICKSTART">
            {copy.quickStart.cta}
          </Link>
        </div>
      </div>
    </Section>
  );
}

function Boundaries({copy}: {copy: HomeCopy}) {
  return (
    <Section
      id="qualified-profile"
      eyebrow={copy.boundaries.eyebrow}
      title={copy.boundaries.title}
      intro={copy.boundaries.intro}>
      <div className={styles.boundaryGrid}>
        <div className={styles.boundaryCard}>
          <span className={styles.goodLabel}>{copy.boundaries.supportedLabel}</span>
          <ul>
            {copy.boundaries.supportedItems.map((item) => <li key={item} dir="auto">{item}</li>)}
          </ul>
        </div>
        <div className={styles.boundaryCard}>
          <span className={styles.limitLabel}>{copy.boundaries.limitsLabel}</span>
          <ul>
            {copy.boundaries.limitItems.map((item) => <li key={item} dir="auto">{item}</li>)}
          </ul>
        </div>
      </div>
      <div className={styles.sectionCta}>
        <Link to="/docs/ENVIRONMENT">{copy.boundaries.link}</Link>
      </div>
    </Section>
  );
}

function Tools({copy}: {copy: HomeCopy}) {
  return (
    <Section
      id="tools"
      eyebrow={copy.tools.eyebrow}
      title={copy.tools.title}
      intro={copy.tools.intro}
      soft>
      <div className={styles.toolList}>
        {copy.tools.items.map(([name, description]) => (
          <div key={name} className={styles.toolRow}>
            <code>{name}</code>
            <span>{description}</span>
          </div>
        ))}
      </div>
      <div className={styles.sectionCta}>
        <Link to="/docs/TOOLS">{copy.tools.link}</Link>
      </div>
    </Section>
  );
}

function Architecture({copy}: {copy: HomeCopy}) {
  return (
    <Section
      id="architecture"
      eyebrow={copy.architecture.eyebrow}
      title={copy.architecture.title}
      intro={copy.architecture.intro}>
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
            <p>{copy.architecture.noteBodies[0]}</p>
          </div>
          <div>
            <strong>local embeddings</strong>
            <p>{copy.architecture.noteBodies[1]}</p>
          </div>
          <div>
            <strong>authorization belongs to FactLane</strong>
            <p>{copy.architecture.noteBodies[2]}</p>
          </div>
          <Link to="/docs/ARCHITECTURE">{copy.architecture.link}</Link>
        </div>
      </div>
    </Section>
  );
}

function FinalCta({copy}: {copy: HomeCopy}) {
  return (
    <section className={styles.finalCta}>
      <div className={styles.container}>
        <div className={styles.finalCtaPanel}>
          <div>
            <div className={styles.eyebrow}>{copy.finalCta.eyebrow}</div>
            <Heading as="h2">{copy.finalCta.title}</Heading>
          </div>
          <div className={styles.finalCtaActions}>
            <Link className="button button--primary button--lg" to="/docs/FAQ">
              {copy.finalCta.fitCta}
            </Link>
            <Link className="button button--secondary button--lg" to="/docs/QUICKSTART">
              {copy.finalCta.setupCta}
            </Link>
            <Link className={styles.textLink} href="https://github.com/Habib1001-m/factlane">
              {copy.finalCta.githubCta}
            </Link>
          </div>
        </div>
      </div>
    </section>
  );
}

export default function Home(): ReactNode {
  const {siteConfig, i18n} = useDocusaurusContext();
  const locale = i18n.currentLocale === 'ar' ? 'ar' : 'en';
  const copy = homeCopy[locale];
  const localeUrl = new URL(siteConfig.baseUrl, siteConfig.url).toString();
  const structuredData = {
    '@context': 'https://schema.org',
    '@type': 'SoftwareSourceCode',
    name: 'FactLane',
    description: copy.meta.structuredDataDescription,
    codeRepository: 'https://github.com/Habib1001-m/factlane',
    programmingLanguage: 'Python',
    runtimePlatform: 'Python 3.11+',
    softwareVersion: '0.1.3',
    license: 'https://www.apache.org/licenses/LICENSE-2.0',
    inLanguage: locale,
    url: localeUrl,
  };

  return (
    <Layout title={copy.meta.title} description={copy.meta.description}>
      <Head>
        <script type="application/ld+json">{JSON.stringify(structuredData)}</script>
      </Head>
      <main>
        <Hero copy={copy} />
        <UseCases copy={copy} />
        <EverydayFit copy={copy} />
        <WhyGoverned copy={copy} />
        <Lifecycle copy={copy} />
        <Authority copy={copy} />
        <QuickStart copy={copy} />
        <Boundaries copy={copy} />
        <Tools copy={copy} />
        <Architecture copy={copy} />
        <FinalCta copy={copy} />
      </main>
    </Layout>
  );
}
