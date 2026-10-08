# Frontend

## Overview
React and TypeScript web interface for the Knowledge Retrieval Engine built with Vite and Tailwind CSS. It provides workspace management, document library browsing, document viewing, and an interactive chat interface with source citations.

## Key files
| File | Owns |
|---|---|
| `src/main.tsx` | React root mounting and DOM attachment |
| `src/App.tsx` | Application router, layout definitions, and navigation redirects |
| `src/pages/ChatPage.tsx` | Multi pane chat workspace with evidence viewing and query input |
| `src/pages/WorkspacePage.tsx` | Workspace listing, selection, and management |
| `src/pages/LibraryPage.tsx` | Document library list with upload access and status indicators |
| `src/pages/DocumentViewerPage.tsx` | Document inspection view supporting PDF and structured records |
| `src/store/useWorkspaceStore.ts` | Zustand store for active workspace state and document collections |
| `src/components/layout/MainLayout.tsx` | Primary two panel application shell with sidebar navigation |

## Commands
```bash
# Install dependencies
npm install

# Run development server
npm run dev

# Build for production
npm run build

# Run linting
npm run lint

# Run unit tests
npm run test
```

## Conventions
* Use React 19, TypeScript, and Vite.
* State management for workspaces and active documents uses Zustand stores.
* Route definitions live in `App.tsx` using React Router.
* Styling uses Tailwind CSS with Radix UI component primitives.
* Lint code with oxlint and test with Vitest.

## Gotchas
* Vite proxies or targets the backend server at port 8001 by default.
* Documents require a valid active workspace before viewing or uploading.

## Related specs
* [docs/UI_UX.md](../docs/UI_UX.md)
* [docs/TECHNICAL_SPEC.md](../docs/TECHNICAL_SPEC.md)

_Drafted by /audit from the repo, worth a quick human pass. Edit freely: once a line stops matching this draft, later runs treat it as curated and will flag rather than overwrite it._
