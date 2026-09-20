FROM node:20-alpine
WORKDIR /app
COPY package.json tsconfig.json next-env.d.ts ./
COPY app ./app
COPY components ./components
COPY lib ./lib
COPY public ./public
RUN npm install && npm run build
CMD ["npm","start"]
