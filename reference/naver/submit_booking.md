# 네이버 예약 직접 제출(`submitBooking`) 조사 기록

조사일 2026-08-01. 대상 상품: `bizes/1498729/items/7094790` (사요나라, 세이코!).
**이 문서의 모든 확인은 로그인 쿠키 없이 수행했고, 예약을 생성하는 요청은 보내지 않았습니다.**

## 결론

예약 제출을 화면 없이 API 한 번으로 재현할 수 있는 경로가 존재합니다. 기존 주석
("네이버 번들에 예약 mutation이 없다")은 `main.js`만 본 결론이었고, 실제 mutation은
지연 로드되는 청크에 있습니다.

```
mobile_components_Booking_Business_shared_bizItem_EntranceTimeAlert_tsx-...-576ed7.42685cba.chunk.js
```

```graphql
mutation submitBooking($input: SubmitBookingParams) {
  submitBooking(input: $input) {
    bookingId
    provider
    url
  }
}
```

엔드포인트는 조회와 같은 `https://m.booking.naver.com/graphql` 입니다.

## 무로그인 프로브 결과

| 보낸 것 | 응답 |
| --- | --- |
| `input: {}` | `Invalid path parameter detected` (BAD_REQUEST) |
| `input: {pengucroProbe: 1}` | `Field "pengucroProbe" is not defined by type "SubmitBookingParams"` |
| `input: {businessId, bizItemId}` | **`Authentication failed`** |
| 현재 페이지와 같은 60개 필드의 완성 페이로드 | GraphQL 타입 검증 통과 후 **`Authentication failed`** |
| `input: {businessTypeId: "EPISODE"}` | `Int cannot represent non-integer value` → `businessTypeId`는 Int |
| `query account { csrfToken }` | 스키마에 존재, 무로그인 시 `account: null` |

`businessId`/`bizItemId`만 넣으면 인증 단계까지 진입합니다. 즉 게이트웨이가 입력으로
REST 경로를 만들고, 그다음 로그인 세션을 검사합니다. 여기서 조사를 멈췄습니다.

스키마 검증 결과 `SubmitBookingParams`의 모든 필드는 nullable입니다(빈 객체가 검증을
통과해 리졸버까지 갔음). 현재 페이지와 같은 완성 페이로드도 필드명과 GraphQL 타입
검증을 통과했습니다. 따라서 나머지 필수값과 상태 판단은 인증 뒤 서버 리졸버가 하며,
필드 구성은 페이지가 보내는 것과 같게 맞춰야 합니다.

## 페이지가 만드는 입력값 (청크의 `ln()` 원본 그대로)

계정에서: `csrfToken`, `isSmsAlarm`
`business`에서: `businessId`, `businessName`(rawNames.name), `serviceName`,
`businessAddressJson`, `bookingTimeUnitCode`, `translateStatusJson`, `businessTypeId`,
`refundPolicyId`, `nPayRegStatusCode`, `businessThumbImage`(businessResources[0].resourceUrl),
`agencyId`/`isAgency`(agencies), `uncompletedBookingProcessCode`,
`uncompletedBookingRefundRate`, `bookingConfirmCode`
`bizItem`에서: `bizItemId`, `bizItemName`, `isPeriodFixed`, `bizItemAddressJson`,
`isSeatUsed`, `isNPayUsed`, `bizItemThumbImage`(resources[0].resourceUrl),
`bookingConfirmCode`, `paymentSettingJson`
선택한 슬롯에서: `slotId`, `slotName`, `slotInfo`, `agencySlotId`, `startDateTime`(ISO),
`endDateTime`, `startDate`, `endDate`, `startMinute`/`endMinute`(EPISODE는 시*60+분),
`hourBit`, `duration`, `bookingCount`, `bizItemPrice`, `price`, `priceTypeJson`
사용자 입력에서: `name`, `phone`, `email`, `birthday`, `requestMessage`,
`customFormInputJson`(인원 선택 등), `visitorName`/`visitorPhone`/`hasVisitor`
고정값: `termsVersion` = `srvAgrVer` 마지막 값 = **`20251030`**,
`globalTimezone: "Asia/Seoul"`, `isAdminBooking: false`, `bookingCondition: ""`,
`language`, `userAgentJson {raw, os, os_version, device}`
그 외: `bookingId`(신규는 null), `optionCategories`, `bookingOptionJson`,
`bookingCouponRequests`, `extraFeeJson {discountPrice, shippingFee, commission}`,
`seatJson`/`seatGroupJson`(좌석 미사용이면 불필요), `isPostPayment`, `shippingStatus`,
`entranceTicket`, `naverPayBackUrl`, `todayDealRate`, `trx`, `arrangementHeadCount`,
`bizItemDailyPriceJson`

이 상품은 `isSeatUsed: false`, `isPeriodFixed: false`, `bookingConfirmCode: "CF01"`
(즉시확정)이므로 좌석·기간·결제 관련 분기는 전부 비활성입니다.

## 서버가 돌려주는 거절 코드 (같은 청크의 에러 핸들러)

- `BizItem is not opened.` — 오픈 전. **오픈 시각은 서버가 제출 시점에 검사합니다.**
- `RT98` — 비정상 예약 시도(어뷰징 판정)
- `RT71`, `RT37`, `RT47`, `RT25` — 예약 불가/상품 제한/판매자 가격 변경
- `BOOKING_NOT_AVAILABLE` + reason: `BOOKING_RESTRICT_BY_BOOKING_CLOSED`,
  `BOOKING_RESTRICT_BY_PERSONAL_LIMITATION`, `FORBID_UNDER_FOURTEEN`,
  `BOOKING_RESTRICT_BY_BIZITEM_IMP_START_DATE_TIME` 등
- `EXCEEDED_AGENCY_BOOKING_LIMIT`, `AgencyServiceNotAvailable`

## 아직 확인하지 못한 것

1. 로그인 세션의 `csrfToken` 검증과 인증 뒤 실제 리졸버 동작. 매진/지난 슬롯도 상태가
   바뀔 가능성을 완전히 배제할 수 없으므로 실제 mutation은 별도 승인 없이 보내지
   않았습니다.
2. `RT98`(비정상 예약) 판정 기준. 화면 흐름을 건너뛴 요청이 여기에 걸릴 위험이 있습니다.
   현재 화면 기반 엔진은 이 위험이 없습니다.
3. `trx` 필드가 일반 예약에서도 검사되는지 (페이지에서는 쿼리스트링에서 오며 보통 없음).
