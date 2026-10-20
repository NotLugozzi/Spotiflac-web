import asyncio
import os
from pydoll.browser import Chrome

async def main():
    print("Launching Pydoll against Turnstile challenge...")
    # Launch browser
    async with Chrome() as browser:
        await browser.start()
        print("Browser started successfully! Printing available methods:")
        print([m for m in dir(browser) if not m.startswith('_')])
        
        # Try to find a page/tab method
        if hasattr(browser, 'new_page'):
            page = await browser.new_page()
        elif hasattr(browser, 'get_page'):
            page = await browser.get_page()
        elif hasattr(browser, 'new_tab'):
            page = await browser.new_tab()
        elif hasattr(browser, 'get_tab'):
            page = await browser.get_tab()
        elif hasattr(browser, 'pages'):
            pages = await browser.pages()
            page = pages[0] if pages else None
        else:
            print("Could not find a method to get a page/tab!")
            return
            
        print(f"Got page/tab object: {page}")
        
        print("Page methods:", [m for m in dir(page) if not m.startswith('_')])
        
        url = "https://api.zarz.moe/v2/challenge?redirect=http://127.0.0.1:1/callback"
        
        print(f"Navigating to {url} ...")
        if hasattr(page, 'go_to'):
            await page.go_to(url)
        elif hasattr(page, 'goto'):
            await page.goto(url)
        
        print("Waiting 5s for Cloudflare challenge widget...")
        await asyncio.sleep(5)
        
        print("Taking click-time screenshot...")
        if hasattr(page, 'take_screenshot'):
            await page.take_screenshot(path="/tmp/turnstile_click_time.png")
        
        print("Waiting 30s for Turnstile token resolution...")
        await asyncio.sleep(30)
        
        print("Taking timeout-time screenshot...")
        if hasattr(page, 'take_screenshot'):
            await page.take_screenshot(path="/tmp/turnstile_timeout.png")
            
        try:
            if hasattr(page, 'current_url'):
                # it might be an async method or a property
                if callable(page.current_url):
                    url_val = await page.current_url()
                else:
                    url_val = page.current_url
                print(f"Final URL: {url_val}")
        except Exception as e:
            print(f"Could not get URL: {e}")

if __name__ == '__main__':
    asyncio.run(main())
