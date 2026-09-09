# Image Downloader Hub

Standalone Flask **universal web + image search** and image downloader application.

## What changed
The normal keyword search now behaves like a web search experience instead of being limited to the built-in categories.

For a search such as `Virat Kohli sports`, the application can show:
- Live web results from SerpAPI using the Google search engine
- Quick-answer/answer-box information when returned by the search provider
- Image results from Pixabay
- News results when returned by SerpAPI
- Video results when returned by SerpAPI
- Load More pagination for additional web/image results
- Original source links that open in a new browser tab

A pasted `http://` or `https://` URL still goes to the existing webpage analyzer, which extracts downloadable images from the target page.

The built-in Nature, City, Technology, Space, Animal and Abstract cards remain as quick-search shortcuts; they no longer restrict what users can search for.

## Setup
```bash
python -m venv venv
# Windows
venv\\Scripts\\activate
# macOS/Linux
# source venv/bin/activate
pip install -r requirements.txt
```

Create a `.env` file in the same directory as `app.py`:

```env
SERPAPI_API_KEY=your_serpapi_api_key
PIXABAY_API_KEY=your_pixabay_api_key
SECRET_KEY=your_long_random_secret
```

Get the API keys from:
- SerpAPI: https://serpapi.com/
- Pixabay: https://pixabay.com/api/docs/

Keep `.env` private and never commit your real API keys to a public repository.

Run:
```bash
python app.py
```

Open:
`http://127.0.0.1:5000`

## Search flow
- Keyword such as `Virat Kohli sports` → SerpAPI web/news/video results + Pixabay images.
- URL such as `https://example.com/gallery` → existing direct webpage image analyzer.
- Category card → performs the same universal search for that category name.

## Notes
SerpAPI is used as the live web-search provider; Pixabay is used for the image gallery. The application does not scrape Google search result HTML directly.

Only scrape/download images and webpages you are authorized to access and use. The URL fetcher blocks private/loopback hosts to reduce SSRF risk.
