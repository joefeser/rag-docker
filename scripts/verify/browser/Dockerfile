# Headless Chromium for the UI checks.
#
# jsdom cannot run this UI: Vite emits <script type="module">, which jsdom will
# not execute, so the page renders as an empty root and every assertion passes
# vacuously. A real browser is the only way these checks mean anything.
FROM node:20-alpine
RUN apk add --no-cache chromium
# Baked in rather than bind-mounted: a scratch directory can be reaped between
# runs, which has already broken this image's dependencies once.
RUN npm install -g puppeteer-core@24
ENV NODE_PATH=/usr/local/lib/node_modules
WORKDIR /w
