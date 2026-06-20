'use client';

import { useRouter } from 'next/navigation';
import { useEffect, useRef, useCallback } from 'react';
import { useAuth } from '@/hooks/useAuth';
import s from './landing.module.css';

/**
 * Landing Page — Swiss Terminal 风格。
 * 未登录用户看到完整产品介绍页，点击「进入控制台」触发 demo 登录。
 * 已登录用户自动跳转 dashboard。
 */
export default function LandingPage() {
  const router = useRouter();
  const { login, isLoading, isAuthenticated, isInitialized } = useAuth();

  // 已登录自动跳转
  useEffect(() => {
    if (isInitialized && isAuthenticated) {
      router.push('/dashboard');
    }
  }, [isInitialized, isAuthenticated, router]);

  const handleLogin = useCallback(async () => {
    try {
      await login();
      router.push('/dashboard');
    } catch (err) {
      console.error('登录失败:', err);
    }
  }, [login, router]);

  // 滚动渐入
  useEffect(() => {
    const els = document.querySelectorAll(`.${s.reveal}`);
    const obs = new IntersectionObserver(
      (entries) => {
        entries.forEach((e) => {
          if (e.isIntersecting) e.target.classList.add(s.visible);
        });
      },
      { threshold: 0.15, rootMargin: '0px 0px -40px 0px' },
    );
    els.forEach((el) => obs.observe(el));
    return () => obs.disconnect();
  }, []);

  // 终端打字动画
  const terminalRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!terminalRef.current) return;
    const el = terminalRef.current;
    const obs = new IntersectionObserver(
      (entries) => {
        if (entries[0].isIntersecting) {
          el.querySelectorAll(`.${s.terminalLine}`).forEach((line) => {
            const delay = Number((line as HTMLElement).dataset.delay) || 0;
            setTimeout(() => line.classList.add(s.typed), delay);
          });
          obs.unobserve(el);
        }
      },
      { threshold: 0.3 },
    );
    obs.observe(el);
    return () => obs.disconnect();
  }, []);

  // 数字计数器
  useEffect(() => {
    const nums = document.querySelectorAll('[data-count]');
    const obs = new IntersectionObserver(
      (entries) => {
        entries.forEach((entry) => {
          if (!entry.isIntersecting) return;
          const el = entry.target as HTMLElement;
          const display = el.dataset.display;
          if (display) { el.textContent = display; obs.unobserve(el); return; }
          const target = parseInt(el.dataset.count!);
          const suffix = el.dataset.suffix || '';
          if (target === 0) { el.textContent = '0'; return; }
          const duration = 1200;
          const start = performance.now();
          const animate = (now: number) => {
            const progress = Math.min((now - start) / duration, 1);
            const eased = 1 - Math.pow(1 - progress, 3);
            el.textContent = Math.round(target * eased) + suffix;
            if (progress < 1) requestAnimationFrame(animate);
          };
          requestAnimationFrame(animate);
          obs.unobserve(el);
        });
      },
      { threshold: 0.5 },
    );
    nums.forEach((el) => obs.observe(el));
    return () => obs.disconnect();
  }, []);

  // 状态机流光
  const flowRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!flowRef.current) return;
    const el = flowRef.current;
    const obs = new IntersectionObserver(
      (entries) => {
        if (!entries[0].isIntersecting) return;
        const nodes = el.querySelectorAll(`.${s.stateNode}`);
        nodes.forEach((node, i) => {
          setTimeout(() => {
            node.classList.add(s.lit);
            if (i < nodes.length - 1) {
              setTimeout(() => node.classList.remove(s.lit), 600);
            }
          }, i * 400);
        });
        obs.unobserve(el);
      },
      { threshold: 0.4 },
    );
    obs.observe(el);
    return () => obs.disconnect();
  }, []);

  if (!isInitialized) return null;
  if (isAuthenticated) return null;

  const scrollTo = (id: string) => {
    document.getElementById(id)?.scrollIntoView({ behavior: 'smooth', block: 'start' });
  };

  return (
    <div className={s.lp}>
      {/* ── Nav ── */}
      <nav className={s.nav}>
        <div className={s.navLogo}>
          <span className={s.dot} />
          HTX Agent Passport
        </div>
        <div className={s.navLinks}>
          <a href="#how" onClick={(e) => { e.preventDefault(); scrollTo('how'); }}>工作原理</a>
          <a href="#features" onClick={(e) => { e.preventDefault(); scrollTo('features'); }}>核心功能</a>
          <a href="#security" onClick={(e) => { e.preventDefault(); scrollTo('security'); }}>安全防线</a>
          <a href="#lifecycle" onClick={(e) => { e.preventDefault(); scrollTo('lifecycle'); }}>生命周期</a>
          <button className={s.navCta} onClick={handleLogin} disabled={isLoading}>
            {isLoading ? '登录中...' : '进入控制台 →'}
          </button>
        </div>
      </nav>

      {/* ── Hero ── */}
      <section className={s.hero}>
        <div>
          <div className={`${s.badge} ${s.reveal}`}>AI Agent Control Plane</div>
          <h1 className={`${s.heroTitle} ${s.reveal} ${s.delay1}`}>
            当 AI 操作真金白银<br />谁来确保它<em>不犯错</em>
          </h1>
          <p className={`${s.heroSub} ${s.reveal} ${s.delay2}`}>
            HTX Agent Passport 是面向加密货币交易的零信任执行框架——LLM 输出仅为提案，每一笔操作须经策略引擎确定性裁决后方可执行。
          </p>
          <div className={`${s.heroActions} ${s.reveal} ${s.delay3}`}>
            <button className={s.btnPrimary} onClick={handleLogin} disabled={isLoading}>
              {isLoading ? '登录中...' : '进入控制台 →'}
            </button>
            <button className={s.btnGhost} onClick={() => scrollTo('how')}>了解更多</button>
          </div>
        </div>

        <div className={`${s.terminal} ${s.reveal} ${s.delay2}`} ref={terminalRef}>
          <div className={s.terminalBar}>
            <span className={s.terminalDot} style={{ background: '#FF5F56' }} />
            <span className={s.terminalDot} style={{ background: '#FFBD2E' }} />
            <span className={s.terminalDot} style={{ background: '#27C93F' }} />
            <span className={s.terminalTitle}>passport-cli</span>
          </div>
          <div className={s.terminalBody}>
            <div className={s.terminalLine} data-delay="200">
              <span className={s.prompt}>$</span> <span className={s.cmd}>passport run &quot;用10 USDT买入BTC&quot;</span>
            </div>
            <div className={s.terminalLine} data-delay="600"><span className={s.dim}>...</span></div>
            <div className={s.terminalLine} data-delay="900"><span className={s.info}>→ 规则路由:</span> 通过</div>
            <div className={s.terminalLine} data-delay="1300"><span className={s.info}>→ B.AI Planner:</span> 生成操作计划</div>
            <div className={s.terminalLine} data-delay="1800"><span className={s.info}>→ Schema 校验:</span> <span className={s.ok}>✓ 有效</span></div>
            <div className={s.terminalLine} data-delay="2200"><span className={s.info}>→ Policy Engine:</span> risk_score=<span className={s.warn}>40</span></div>
            <div className={s.terminalLine} data-delay="2700"><span className={s.warn}>→ APPROVAL_REQUIRED</span></div>
            <div className={s.terminalLine} data-delay="3200"><span className={s.info}>→ 用户审批:</span> <span className={s.ok}>✓ 已确认</span></div>
            <div className={s.terminalLine} data-delay="3600"><span className={s.info}>→ 执行网关:</span> 二次校验通过</div>
            <div className={s.terminalLine} data-delay="4000"><span className={s.dim}>...</span></div>
            <div className={s.terminalLine} data-delay="4300">
              <span className={s.ok}>✓ EXECUTED</span> <span className={s.dim}>action_id: 1fe87dff...</span>
            </div>
          </div>
        </div>
      </section>

      {/* ── Metrics ── */}
      <div className={s.metrics}>
        {[
          { count: 3, label: '纵深安全防线' },
          { count: 7, label: '策略裁决步骤' },
          { count: 100, suffix: '%', label: '操作可审计' },
          { count: 0, display: 'Zero', label: '密钥明文暴露' },
        ].map((m, i) => (
          <div key={i} className={`${s.metricItem} ${s.reveal} ${i > 0 ? s[`delay${i}`] : ''}`}>
            <div className={s.metricNum} data-count={m.count} data-suffix={m.suffix || ''} data-display={m.display || ''}>
              {m.display || '0'}
            </div>
            <div className={s.metricLabel}>{m.label}</div>
          </div>
        ))}
      </div>

      {/* ── How It Works ── */}
      <section className={s.howSection} id="how">
        <div className={s.howInner}>
          <div className={`${s.sectionLabel} ${s.reveal}`}>How It Works</div>
          <h2 className={`${s.sectionTitle} ${s.reveal} ${s.delay1}`}>从意图到执行，四步闭环</h2>
          <p className={`${s.sectionDesc} ${s.reveal} ${s.delay2}`}>
            每一个操作请求都经过严格的多层校验——没有任何环节可以跳过。
          </p>
          <div className={s.howSteps}>
            {[
              { num: '1', title: '感知意图', desc: '用户输入自然语言，规则路由进行第一层语义过滤，拦截提现等高危意图。' },
              { num: '2', title: 'AI 规划', desc: 'B.AI 大模型生成结构化操作计划，Schema 校验杜绝幻觉，归一化金额与币种。' },
              { num: '3', title: '策略裁决', desc: 'Policy Engine 七步确定性检查：单笔限额、日累计、币种白名单、风险评分。' },
              { num: '4', title: '安全执行', desc: '人工审批 → 执行网关二次校验 → Stale Price 检测 → 调用 HTX API。' },
            ].map((step, i) => (
              <div key={i} className={`${s.howStep} ${s.reveal} ${s[`delay${i + 1}`] || ''}`}>
                <div className={s.howStepNum}>{step.num}</div>
                <div className={s.howStepTitle}>{step.title}</div>
                <div className={s.howStepDesc}>{step.desc}</div>
              </div>
            ))}
          </div>
        </div>
      </section>

      {/* ── Features ── */}
      <section className={s.section} id="features">
        <div className={`${s.sectionLabel} ${s.reveal}`}>Core Capabilities</div>
        <h2 className={`${s.sectionTitle} ${s.reveal} ${s.delay1}`}>不只是工具，是信任基础设施</h2>
        <p className={`${s.sectionDesc} ${s.reveal} ${s.delay2}`}>
          六项核心能力构建端到端的安全执行链路——从意图感知到策略裁决，从审批兜底到审计追溯。
        </p>
        <div className={s.featuresGrid}>
          {[
            { num: '01', icon: '⊘', title: '零信任执行', desc: 'LLM 的输出仅是一份提案。任何操作都必须经过 Policy Engine 的七步确定性裁决，杜绝 AI 幻觉带来的资金风险。' },
            { num: '02', icon: '⧫', title: '信封加密', desc: 'API 密钥通过 Vault 主密钥加密存储，运行时解密、用后即焚。密钥从不以明文形式出现在数据库或日志中。' },
            { num: '03', icon: '⌬', title: 'Merkle 审计链', desc: '每一次状态变更、策略裁决、执行结果都记录为审计事件，形成哈希链 + 定期 STH 锚定，可验证不可篡改。' },
            { num: '04', icon: '☰', title: '人工审批兜底', desc: '高危操作自动触发 HITL 审批流程。用户可通过前端界面确认或拒绝，超时自动过期。' },
            { num: '05', icon: '◎', title: '策略热更新', desc: 'Passport 策略支持运行时调整——单笔限额、日累计限额、允许币种均可通过前端实时配置，无需重启。' },
            { num: '06', icon: '⟁', title: 'Stale Price 防护', desc: '审批到执行之间若市场剧烈波动，执行网关自动检测价格偏离度。超过阈值时阻断执行，避免滑点损失。' },
          ].map((f, i) => (
            <div key={i} className={`${s.featureCard} ${s.reveal} ${i % 3 > 0 ? s[`delay${i % 3}`] : ''}`}>
              <div className={s.featureNum}>{f.num}</div>
              <div className={s.featureIcon}>{f.icon}</div>
              <div className={s.featureTitle}>{f.title}</div>
              <div className={s.featureDesc}>{f.desc}</div>
            </div>
          ))}
        </div>
      </section>

      {/* ── Security ── */}
      <section className={s.securitySection} id="security">
        <div className={s.securityInner}>
          <div className={`${s.sectionLabel} ${s.reveal}`}>Defense in Depth</div>
          <h2 className={`${s.sectionTitle} ${s.reveal} ${s.delay1}`} style={{ color: 'var(--lp-bg)' }}>三道纵深防线</h2>
          <p className={`${s.sectionDesc} ${s.reveal} ${s.delay2}`}>
            每一层独立运作、互为冗余。即使某一层失效，后续层仍能拦截风险。
          </p>
          <div className={s.defenseFlow}>
            {[
              { step: '1', label: 'Layer 01', title: '规则路由拦截', desc: '基于关键词和语义的第一道过滤——提现、借贷、跨链转账等高危意图在进入 AI 规划前即被硬拦截。' },
              { step: '2', label: 'Layer 02', title: 'Policy Engine 裁决', desc: '纯函数策略引擎，对操作计划执行七步确定性检查：金额、币种、日限额、风险评分、幂等性。' },
              { step: '3', label: 'Layer 03', title: '执行网关校验', desc: '审批后、执行前的最终安全门——聚合当日真实累计、检测市场价格偏离、验证幂等性。' },
            ].map((d, i) => (
              <div key={i} style={{ display: 'contents' }}>
                {i > 0 && <div className={s.defenseArrow}>→</div>}
                <div className={`${s.defenseCard} ${s.reveal} ${i > 0 ? s[`delay${i}`] : ''}`} data-step={d.step}>
                  <div className={s.defenseLabel}>{d.label}</div>
                  <div className={s.defenseTitle}>{d.title}</div>
                  <div className={s.defenseDesc}>{d.desc}</div>
                </div>
              </div>
            ))}
          </div>
        </div>
      </section>

      {/* ── Lifecycle ── */}
      <section className={s.stateSection} id="lifecycle">
        <div className={`${s.sectionLabel} ${s.reveal}`}>Action Lifecycle</div>
        <h2 className={`${s.sectionTitle} ${s.reveal} ${s.delay1}`}>每一步都有据可查</h2>
        <p className={`${s.sectionDesc} ${s.reveal} ${s.delay2}`}>
          Action 从请求到终态的完整生命周期，所有状态转换均经审计事件记录。
        </p>

        <div className={s.stateFlow} ref={flowRef}>
          {['REQUESTED', 'PLANNING', 'PLAN_VALIDATED', 'RISK_CHECKING', 'APPROVAL_REQUIRED', 'APPROVED', 'EXECUTING', 'EXECUTED'].map((state, i) => (
            <div key={state} style={{ display: 'contents' }}>
              {i > 0 && <div className={s.stateConnector} />}
              <div className={s.stateNode}>{state}</div>
            </div>
          ))}
        </div>

        <div className={s.stateBranch}>
          <div className={s.stateNode} style={{ opacity: 0.4 }}>RISK_CHECKING</div>
          <div className={s.stateConnector} />
          <div className={`${s.stateNode} ${s.reject}`}>AUTO_REJECTED</div>
          <span className={s.stateBranchLabel}>策略违规 → 终态</span>
        </div>
        <div className={s.stateBranch}>
          <div className={s.stateNode} style={{ opacity: 0.4 }}>APPROVAL_REQUIRED</div>
          <div className={s.stateConnector} />
          <div className={`${s.stateNode} ${s.reject}`}>REJECTED_BY_USER</div>
          <span className={s.stateBranchLabel}>用户拒绝 → 终态</span>
        </div>
      </section>

      {/* ── Trust Bar ── */}
      <div className={s.trustBar}>
        {['FastAPI', 'Next.js 14', 'PostgreSQL', 'Ed25519', 'Merkle Tree', 'Alembic'].map((t) => (
          <div key={t} className={s.trustItem}>
            <span className={s.trustDot} />{t}
          </div>
        ))}
      </div>

      {/* ── CTA ── */}
      <section className={s.ctaSection}>
        <h2 className={`${s.ctaTitle} ${s.reveal}`}>
          让 AI 在<em>信任的边界</em>内<br />自由行动
        </h2>
        <p className={`${s.ctaDesc} ${s.reveal} ${s.delay1}`}>
          HTX Agent Passport——权限、风险、审计三位一体，为 AI 代理构建可信赖的执行环境。
        </p>
        <div className={`${s.ctaActions} ${s.reveal} ${s.delay2}`}>
          <button className={s.btnPrimary} onClick={handleLogin} disabled={isLoading}>
            {isLoading ? '登录中...' : '开始使用 →'}
          </button>
          <button className={s.btnGhost}>GitHub</button>
        </div>
      </section>

      {/* ── Footer ── */}
      <footer className={s.footer}>
        <span>HTX Agent Passport · AI Agent Control Plane</span>
        <span>Built with FastAPI + Next.js + PostgreSQL</span>
      </footer>
    </div>
  );
}
