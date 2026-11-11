import { StdioServerTransport } from '@modelcontextprotocol/sdk/server/stdio.js';
import { createOamClient } from './oam-client.ts';
import { createOamMcpServer } from './server.ts';

async function main(): Promise<void> {
  const client = createOamClient({
    baseUrl: process.env.OAM_BASE_URL ?? '',
    userId: process.env.OAM_USER_ID ?? '',
    apiKey: process.env.OAM_USER_API_KEY ?? '',
  });
  const server = createOamMcpServer(client);
  await server.connect(new StdioServerTransport());
}

main().catch((error: unknown) => {
  const message = error instanceof Error ? error.message : 'failed to start';
  process.stderr.write(`oam-mcp: ${message}\n`);
  process.exitCode = 1;
});
