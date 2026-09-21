# Official Node multi-platform manifest, resolved from Docker Registry.
FROM node:24-alpine@sha256:ebfe2f90462722a7a4de65e91990e97fe0d401c70e0e762c5b53302f905ec1c1 AS build
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci --ignore-scripts --no-audit --no-fund
COPY scripts/build.mjs scripts/smoke.mjs scripts/
COPY src/ src/
COPY tests/*.test.mjs tests/
RUN npm test
ARG BUILD_REVISION=development
RUN BUILD_REVISION="$BUILD_REVISION" npm run build

FROM node:24-alpine@sha256:ebfe2f90462722a7a4de65e91990e97fe0d401c70e0e762c5b53302f905ec1c1
WORKDIR /app
# Package managers are build tools; the runtime executes only the compiled app.
RUN rm -rf /usr/local/lib/node_modules/npm /opt/yarn-* \
    && rm -f /usr/local/bin/npm /usr/local/bin/npx /usr/local/bin/yarn /usr/local/bin/yarnpkg \
    && addgroup -g 10001 app && adduser -D -H -u 10001 -G app app
COPY --from=build --chown=10001:10001 /app/dist/ ./dist/
USER 10001:10001
ENV NODE_ENV=production PORT=8080
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 CMD ["node", "-e", "fetch('http://127.0.0.1:'+(process.env.PORT||'8080')+'/healthz').then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))"]
CMD ["node", "dist/main.mjs"]
