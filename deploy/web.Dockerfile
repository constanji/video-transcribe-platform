FROM node:20-alpine
WORKDIR /app
# NEXT_PUBLIC_* is inlined at build time; it cannot be changed at runtime.
ARG NEXT_PUBLIC_API_URL
ARG NPM_REGISTRY=https://registry.npmjs.org
ENV NEXT_PUBLIC_API_URL=${NEXT_PUBLIC_API_URL}
COPY package.json package-lock.json tsconfig.json next-env.d.ts ./
COPY app ./app
COPY components ./components
COPY lib ./lib
COPY public ./public
RUN npm config set registry "${NPM_REGISTRY}" \
    && npm install \
    && npm run build
ENV NODE_ENV=production
CMD ["npm","start"]
