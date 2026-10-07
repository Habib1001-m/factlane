import React from 'react';
import type {ReactNode} from 'react';
import clsx from 'clsx';
import Head from '@docusaurus/Head';
import Link from '@docusaurus/Link';
import useDocusaurusContext from '@docusaurus/useDocusaurusContext';
import Layout from '@theme/Layout';
import Heading from '@theme/Heading';
import {
  homeCopy,
  landingExperience,
  type HomeCopy,
  type LandingExperience,
} from '../content/homeCopy';
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
            <a className="button button--primary button--lg" href="#fit-check">
              {copy.hero.primaryCta}
            </a>
            <a className="button button--secondary button--lg" href="#agent-onboarding">
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

function FitCheck({copy}: {copy: LandingExperience['fitCheck']}) {
  return (
    <section id="fit-check" className={styles.fitCheckSection}>
      <div className={styles.container}>
        <div className={styles.fitCheckPanel}>
          <div className={styles.fitCheckHeading}>
            <div className={styles.eyebrow}>{copy.eyebrow}</div>
            <Heading as="h2">{copy.title}</Heading>
            <p>{copy.intro}</p>
          </div>
          <div className={styles.fitCheckGrid}>
            {copy.items.map((item, index) => (
              <article key={item.title} className={styles.fitCheckCard}>
                <span className={styles.fitCheckNumber}>0{index + 1}</span>
                <Heading as="h3">{item.title}</Heading>
                <p>{item.body}</p>
              </article>
            ))}
          </div>
          <div className={styles.fitCheckFooter}>
            <p>{copy.boundary}</p>
            <a href="#use-cases">{copy.action}</a>
          </div>
        </div>
      </div>
    </section>
  );
}

function ProductDifference({copy}: {copy: LandingExperience['differentiation']}) {
  return (
    <Section
      id="why-different"
      eyebrow={copy.eyebrow}
      title={copy.title}
      intro={copy.intro}>
      <div className={styles.differenceGrid}>
        {copy.cards.map((card, index) => (
          <article key={card.title} className={styles.differenceCard}>
            <span className={styles.trustNumber}>0{index + 1}</span>
            <Heading as="h3">{card.title}</Heading>
            <p>{card.body}</p>
          </article>
        ))}
      </div>
      <div className={styles.sectionCta}>
        <Link to="/docs/CORE_CONCEPTS">{copy.docsAction}</Link>
      </div>
    </Section>
  );
}

function MemoryCoexistence({copy}: {copy: LandingExperience['coexistence']}) {
  return (
    <Section
      id="memory-stack"
      eyebrow={copy.eyebrow}
      title={copy.title}
      intro={copy.intro}
      soft>
      <div className={styles.memoryStackGrid}>
        {copy.lanes.map((lane) => (
          <article key={lane.label} className={styles.memoryStackCard}>
            <span>{lane.label}</span>
            <Heading as="h3">{lane.title}</Heading>
            <p>{lane.body}</p>
          </article>
        ))}
      </div>
      <div className={styles.stackNote}>{copy.note}</div>
      <div className={styles.sectionCta}>
        <Link to="/docs/ARCHITECTURE">{copy.docsAction}</Link>
      </div>
    </Section>
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

function EverydayFit({copy, locale = 'en'}: {copy: HomeCopy; locale?: 'en' | 'ar'}) {
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
        {locale === 'ar' ? (
          <a className="button button--primary" href="#agent-onboarding">
            {copy.everydayFit.beginnerCta}
          </a>
        ) : (
          <Link className="button button--primary" to="/docs/">
            {copy.everydayFit.beginnerCta}
          </Link>
        )}
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

async function copyText(text: string): Promise<void> {
  if (navigator.clipboard?.writeText) {
    try {
      await navigator.clipboard.writeText(text);
      return;
    } catch {
      // Fall through to the document-level copy path for restricted browser contexts.
    }
  }

  const textarea = document.createElement('textarea');
  textarea.value = text;
  textarea.setAttribute('readonly', '');
  textarea.style.position = 'fixed';
  textarea.style.opacity = '0';
  document.body.appendChild(textarea);
  textarea.select();
  const copied = document.execCommand('copy');
  document.body.removeChild(textarea);
  if (!copied) {
    throw new Error('Copy command was not accepted by the browser');
  }
}

function AgentOnboarding({copy}: {copy: LandingExperience['onboarding']}) {
  const [copyState, setCopyState] = React.useState<'idle' | 'copied' | 'failed'>('idle');

  const handleCopy = async () => {
    try {
      await copyText(copy.promptBody);
      setCopyState('copied');
      window.setTimeout(() => setCopyState('idle'), 2400);
    } catch {
      setCopyState('failed');
    }
  };

  return (
    <Section
      id="agent-onboarding"
      eyebrow={copy.eyebrow}
      title={copy.title}
      intro={copy.intro}
      soft>
      <div className={styles.agentOnboardingLayout}>
        <div className={styles.agentSteps}>
          {copy.steps.map((step, index) => (
            <article key={step.title} className={styles.agentStep}>
              <span className={styles.agentStepNumber}>0{index + 1}</span>
              <div>
                <Heading as="h3">{step.title}</Heading>
                <p>{step.body}</p>
              </div>
            </article>
          ))}
          <aside className={styles.capabilityNote}>
            <strong>{copy.capabilityTitle}</strong>
            <p>{copy.capabilityBody}</p>
          </aside>
        </div>

        <aside className={styles.agentPromptCard} aria-labelledby="agent-prompt-title">
          <div className={styles.agentPromptHeader}>
            <div>
              <span>{copy.promptLabel}</span>
              <Heading id="agent-prompt-title" as="h3">{copy.promptTitle}</Heading>
            </div>
            <span className={styles.promptBadge}>{copy.badge}</span>
          </div>
          <div className={styles.agentPromptText} dir={copy.promptDirection}>{copy.promptBody}</div>
          <div className={styles.agentPromptActions}>
            <button className="button button--primary" type="button" onClick={handleCopy}>
              {copyState === 'copied' ? copy.copiedAction : copy.copyAction}
            </button>
            <Link
              className="button button--secondary"
              href="https://github.com/Habib1001-m/factlane">
              {copy.githubAction}
            </Link>
          </div>
          <div className={styles.copyStatus} aria-live="polite">
            {copyState === 'copied'
              ? copy.copiedAction
              : copyState === 'failed'
                ? copy.copyFailedAction
                : ''}
          </div>
          <Link className={styles.manualSetupLink} to="/docs/QUICKSTART">
            {copy.manualAction}
          </Link>
        </aside>
      </div>
    </Section>
  );
}

function CompactTrust({copy}: {copy: LandingExperience['trust']}) {
  return (
    <Section id="how-it-works" eyebrow={copy.eyebrow} title={copy.title} intro={copy.intro}>
      <div className={styles.arabicTrustGrid}>
        {copy.cards.map((card, index) => (
          <article key={card.title} className={styles.arabicTrustCard}>
            <span className={styles.trustNumber}>0{index + 1}</span>
            <Heading as="h3">{card.title}</Heading>
            <p>{card.body}</p>
          </article>
        ))}
      </div>
      <div className={styles.trustFlow} aria-label="Candidate to Current">
        {copy.flow.map((item, index) => (
          <React.Fragment key={item.label}>
            <div className={styles.trustFlowItem}>
              <strong><bdi dir="auto">{item.label}</bdi></strong>
              <span>{item.body}</span>
            </div>
            {index < copy.flow.length - 1 && <span className={styles.trustFlowArrow} aria-hidden="true">←</span>}
          </React.Fragment>
        ))}
      </div>
      <div className={styles.sectionCta}>
        <Link to="/docs/CORE_CONCEPTS">{copy.docsAction}</Link>
      </div>
    </Section>
  );
}

function EngineeringRigor({
  copy,
  whyNow,
}: {
  copy: LandingExperience['rigor'];
  whyNow: LandingExperience['whyNow'];
}) {
  return (
    <Section
      id="engineering-rigor"
      eyebrow={copy.eyebrow}
      title={copy.title}
      intro={copy.intro}
      soft>
      <div className={styles.rigorGrid}>
        {copy.cards.map((card, index) => (
          <article key={card.title} className={styles.rigorCard}>
            <span className={styles.trustNumber}>0{index + 1}</span>
            <Heading as="h3">{card.title}</Heading>
            <p>{card.body}</p>
          </article>
        ))}
      </div>
      <p className={styles.qualificationLine}>{copy.qualification}</p>
      <div className={styles.rigorActions}>
        <Link to="/docs/ARCHITECTURE">{copy.architectureAction}</Link>
        <Link to="/docs/SECURITY">{copy.securityAction}</Link>
      </div>
      <aside className={styles.whyNowPanel}>
        <div className={styles.cardEyebrow}>{whyNow.eyebrow}</div>
        <Heading as="h3">{whyNow.title}</Heading>
        <p>{whyNow.body}</p>
        <strong>{whyNow.caveat}</strong>
      </aside>
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
            <a className="button button--primary button--lg" href="#agent-onboarding">
              {copy.finalCta.fitCta}
            </a>
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

function ProductLanding({
  copy,
  experience,
}: {
  copy: HomeCopy;
  experience: LandingExperience;
}) {
  return (
    <>
      <Hero copy={copy} />
      <FitCheck copy={experience.fitCheck} />
      <ProductDifference copy={experience.differentiation} />
      <AgentOnboarding copy={experience.onboarding} />
      <UseCases copy={copy} />
      <MemoryCoexistence copy={experience.coexistence} />
      <CompactTrust copy={experience.trust} />
      <EngineeringRigor copy={experience.rigor} whyNow={experience.whyNow} />
      <FinalCta copy={copy} />
    </>
  );
}

export default function Home(): ReactNode {
  const {siteConfig, i18n} = useDocusaurusContext();
  const locale = i18n.currentLocale === 'ar' ? 'ar' : 'en';
  const copy = homeCopy[locale];
  const experience = landingExperience[locale];
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
        <ProductLanding copy={copy} experience={experience} />
      </main>
    </Layout>
  );
}
