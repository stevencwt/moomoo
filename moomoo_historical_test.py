import moomoo as mm

quote_ctx = mm.OpenQuoteContext(host='127.0.0.1', port=11111)

ret, data, page_req_key = quote_ctx.request_history_kline(
    'US.TSLA',
    start='2026-02-01',
    end='2026-02-21',
    ktype=mm.KLType.K_DAY
)

if ret == mm.RET_OK:
    print("✅ Historical candles available!")
    print(data[['time_key','open','high','low','close','volume']].tail(10))
else:
    print(f"❌ Failed: {data}")

quote_ctx.close()