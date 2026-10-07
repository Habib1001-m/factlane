import React from 'react';
import type {ReactNode} from 'react';
import Link from '@docusaurus/Link';
import useDocusaurusContext from '@docusaurus/useDocusaurusContext';
import Layout from '@theme/Layout';
import Heading from '@theme/Heading';
import answerAuthority from '../content/answerAuthority.json';
import styles from './answers.module.css';

type Locale = 'en' | 'ar';

export default function Answers(): ReactNode {
  const {i18n} = useDocusaurusContext();
  const locale: Locale = i18n.currentLocale === 'ar' ? 'ar' : 'en';
  const copy = answerAuthority[locale];
  const onboardingHref = locale === 'ar' ? '/ar/#agent-onboarding' : '/#agent-onboarding';

  return (
    <Layout title={copy.meta.title} description={copy.meta.description}>
      <main className={styles.page}>
        <header className={styles.hero}>
          <div className={styles.container}>
            <div className={styles.eyebrow}>{copy.eyebrow}</div>
            <Heading as="h1">{copy.title}</Heading>
            <p className={styles.intro}>{copy.intro}</p>
            <p className={styles.sourceCue}>{copy.sourceCue}</p>
            <ul className={styles.facts} aria-label={copy.factsLabel}>
              {copy.facts.map((fact) => (
                <li key={fact.strong + fact.rest}>
                  <strong><bdi dir="auto">{fact.strong}</bdi></strong>
                  {fact.rest}
                </li>
              ))}
            </ul>
          </div>
        </header>

        <section className={styles.answersSection} aria-label={copy.eyebrow}>
          <div className={styles.container}>
            <div className={styles.answerGrid}>
              {copy.answers.map((item, index) => (
                <article
                  className={styles.answerCard}
                  id={item.id}
                  data-answer-id={item.id}
                  key={item.id}>
                  <div className={styles.answerNumber}>{String(index + 1).padStart(2, '0')}</div>
                  <Heading as="h2">{item.question}</Heading>
                  <p className={styles.answerText}>{item.answer}</p>
                  <div className={styles.sources}>
                    <strong>{copy.sourcesLabel}</strong>
                    <ul>
                      {item.sources.map((source) => (
                        <li key={source.to}>
                          <Link to={source.to}>{source.label}</Link>
                        </li>
                      ))}
                    </ul>
                  </div>
                </article>
              ))}
            </div>
          </div>
        </section>

        <section className={styles.cta}>
          <div className={styles.container}>
            <div className={styles.ctaPanel}>
              <div>
                <div className={styles.eyebrow}>{copy.cta.eyebrow}</div>
                <Heading as="h2">{copy.cta.title}</Heading>
                <p>{copy.cta.body}</p>
              </div>
              <div className={styles.ctaActions}>
                <a
                  className="button button--primary button--lg"
                  data-noBrokenLinkCheck
                  href={onboardingHref}>
                  {copy.cta.agent}
                </a>
                <Link className="button button--secondary button--lg" to="/docs/QUICKSTART/">
                  {copy.cta.quickstart}
                </Link>
              </div>
            </div>
          </div>
        </section>
      </main>
    </Layout>
  );
}
