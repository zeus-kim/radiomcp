# Station Takedown Policy

RadioMCP respects the rights of broadcasters and content owners. If you are a station owner or authorized representative and wish to have your station removed from RadioMCP, we provide a simple and transparent process.

## How It Works

RadioMCP maintains a public `blocklist.json` that is automatically synced to all clients. When a station is added to this blocklist, it is removed from search results and cannot be played through any RadioMCP client.

### Dual Enforcement

Takedown requests are enforced at two levels:

1. **GitHub Blocklist (Remote Sync)** — All RadioMCP clients periodically fetch `blocklist.json` from this repository. Blocked stations are automatically purged from local databases.

2. **Server-Side Blocklist (relay4 API)** — The RadioMCP server also maintains its own blocklist that filters stations before they reach any client.

This dual-layer approach ensures that blocked stations are removed even if one system experiences delays.

## How to Request a Takedown

### Option 1: GitHub Issue (Recommended)

1. Go to [Issues](https://github.com/meshpop/radiomcp/issues/new/choose)
2. Select **"Station Takedown Request"**
3. Fill in your station details
4. Submit — your station will be automatically added to the blocklist

The GitHub Action will process your request, update `blocklist.json`, and close the issue. All RadioMCP clients will pick up the change within 1 hour.

### Option 2: Email

Send a takedown request to **mpop@mpop.dev** with:
- Station name
- Stream URL (if known)
- Proof of ownership or authorization

## Timeline

- **GitHub Issue**: Processed automatically within minutes
- **Email**: Processed within 48 hours
- **Client sync**: All clients sync the blocklist hourly

## Reversal

If you change your mind, open a new issue or email us to request removal from the blocklist.

## Contact

- Email: mpop@mpop.dev
- GitHub: https://github.com/meshpop/radiomcp/issues
