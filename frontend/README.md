# HTX Agent Passport — Frontend

Next.js 14（App Router） + TypeScript + Tailwind CSS 实现的演示控制台。
负责登录、凭证管理、护照向导、任务编排、审批弹窗、审计重放等界面。

## 目录结构

```
frontend/
├── package.json
├── tsconfig.json
├── tailwind.config.ts
├── postcss.config.js
├── next.config.js
└── src/
    ├── app/
    │   ├── layout.tsx       # 根布局
    │   ├── page.tsx         # 首页 / 登录入口（占位）
    │   └── globals.css      # Tailwind 入口
    └── lib/
        ├── api.ts           # fetch 客户端基础结构
        └── types.ts         # 与后端镜像的 TypeScript 类型
```

## 本地开发

> 任务 1 仅创建脚手架，**不执行依赖安装**。

```bash
# 1. 安装依赖
npm install

# 2. 配置后端地址（可选，默认 http://localhost:8000）
echo "NEXT_PUBLIC_API_BASE_URL=http://localhost:8000" > .env.local

# 3. 启动开发服务器
npm run dev

# 4. 类型检查 / lint
npm run type-check
npm run lint

# 5. 生产构建
npm run build && npm run start
```

## 设计参考

UI 流程见 design.md「Components and Interfaces」与方法论 §14（分层反馈）。
后续任务（16-18）会陆续实现：

- 任务 16  登录 / 仪表盘 / 凭证管理 / 护照向导 / Policy 编辑器
- 任务 17  TaskComposer / ApprovalModal / FeedbackLayer / useActionPolling
- 任务 18  AuditTimeline 审计重放界面
