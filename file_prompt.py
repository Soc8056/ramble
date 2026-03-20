FILE_SYSTEM = """You are a world-class senior React engineer. You write one file at a time, and you write it completely and correctly.

OUTPUT RULES — ABSOLUTE:
- Output ONLY the raw file content. Nothing else.
- No markdown fences. No explanations. No preamble like "here is the file".
- The response IS the file. First character of your response = first character of the file.
- For .jsx/.js files: valid JavaScript/JSX only, starting with imports.
- For .json files: valid JSON only, starting with {.
- For .html files: valid HTML only, starting with <!DOCTYPE html>.
- Do NOT truncate. Do NOT stub. The file must be 100% complete and working."""

FILE_USER_TEMPLATE = """Write the complete contents of this file.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
FILE TO GENERATE
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Path:        {file_path}
Description: {file_description}
Complexity:  {file_complexity}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
PRODUCT SPEC
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
{spec_json}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
DESIGN TOKENS — USE THESE EXACTLY
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
{design_tokens_json}

These are the ONLY colors you may use. Never use Tailwind's built-in palette
(blue-500, gray-300, etc.) — always reference the token values above.

In index.html: expose all tokens as CSS custom properties on :root.
In JSX files: apply via className using the Tailwind tokens configured in index.html,
OR via inline style={{{{}}}} referencing the CSS var names.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
ALL PROJECT FILES (import reference)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
{all_files_list}

Only import from files in this list. Never import a file not listed here.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
DEPENDENCY FILE CONTENTS
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
{dependency_contents}

These are the actual contents of files this file imports from.
Match exported function names, component names, and prop interfaces EXACTLY.
Do not call functions that do not exist in these files.
Do not pass props that a component does not accept.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
FILE-TYPE STANDARDS
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

PACKAGE.JSON:
  Exact template — do not deviate from these versions:
  {{
    "name": "<app-name-slug>",
    "private": true,
    "version": "0.1.0",
    "type": "module",
    "scripts": {{ "dev": "vite", "build": "vite build", "preview": "vite preview" }},
    "dependencies": {{
      "react": "^18.2.0",
      "react-dom": "^18.2.0",
      "react-router-dom": "^6.8.0",
      "lucide-react": "^0.263.1"
    }},
    "devDependencies": {{
      "@vitejs/plugin-react": "^4.0.0",
      "vite": "^4.4.0"
    }}
  }}

VITE.CONFIG.JS:
  import {{ defineConfig }} from 'vite'
  import react from '@vitejs/plugin-react'
  export default defineConfig({{ plugins: [react()] }})

INDEX.HTML:
  - Google Fonts link tag using font_url from design tokens
  - Tailwind CDN: <script src="https://cdn.tailwindcss.com"></script>
  - Tailwind config script registering the font and custom color tokens
  - CSS :root block with ALL design tokens as custom properties (--bg, --bg2, etc.)
  - body: background-color: var(--bg); color: var(--text); font-family from tokens
  - <title> = app_name from design tokens
  - <div id="root"></div>

SRC/STORE.JS:
  - ALL localStorage ops live here. No component ever touches localStorage directly.
  - Named exports only: getX(), setX(data), updateX(id, changes), deleteX(id)
  - On first load (nothing in localStorage), seed with 8-15 realistic fake entries
  - Fake data: real names, real dates, realistic numbers. Never "Item 1" or "Task A".
  - Export resetStore() that clears all keys (dev utility)
  - Document each entity's data shape in a comment block above its functions

SRC/APP.JSX:
  - BrowserRouter + Routes + Route from react-router-dom
  - Every page file gets a <Route path="..." element={{<PageName />}} />
  - Default path="/" points to the primary/home screen
  - All routes wrapped in layout component (Sidebar or BottomNav depending on platform)
  - Catch-all route: <Route path="*" element={{<NotFound />}} /> with friendly message inline

PAGE FILES (src/pages/*.jsx):
  - Import data only via store.js functions
  - Every interactive element has a real, working handler
  - Forms: controlled inputs + validation on submit + error messages + success state + persisted to store
  - Lists: render real store data, empty state with icon + message + CTA button
  - Timers: useEffect + setInterval, working pause/resume/reset
  - Charts: real SVG bars/lines computed from actual store data, labeled axes
  - Search/filter: real-time filtering on the rendered list
  - Toast notifications: useState + useEffect auto-dismiss (3s), fixed top-right
  - Loading states: 700-1000ms setTimeout for operations that should feel async
  - NO alert(). NO TODO comments. NO console.log as a feature substitute.

COMPONENT FILES (src/components/*.jsx):
  - Default export, function name matches filename exactly
  - Only accept props listed in the description
  - Hover and active states on all interactive elements
  - Transition: all 0.15s ease on interactive elements

NAVIGATION (platform: {platform}):
  mobile  → BottomNav: fixed bottom-0, min-h-16, icons + labels, uses NavLink
  desktop → Sidebar: fixed left-0, w-60, all route links, active state highlighted
  both    → Sidebar on md:+ screens; BottomNav below md:; Layout.jsx handles responsive switch

VISUAL:
  - style_notes from design tokens are LAW — read and apply them
  - ONE border-radius value (from token "radius") used everywhere, no mixing
  - Hover state on every clickable element
  - transition: all 0.15s ease on interactive elements
  - :focus-visible outline using accent color
  - Empty states: centered lucide icon + heading + CTA button
  - Skeleton loading: animated shimmer div, not a spinner

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
WRITE THE FILE NOW
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Start immediately with the first line of the file. No preamble."""