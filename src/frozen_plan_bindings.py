"""External trust root for the immutable PlanOnly.

The plan pins the SHA-256 of the runtime, and the runtime verifies itself against the
plan. That loop is closed and proves nothing on its own: editing both together keeps
it perfectly consistent, which is exactly what a legitimate reissue does and exactly
what an illegitimate one would do too.

This module is deliberately tiny, is NOT part of the bound implementation set, and
pins the one plan identity the runtime is allowed to load. Changing it is a visible,
reviewable act in its own right.
"""

ACTIVE_PLAN = {
    "schema": "premarket_perp_capture_planonly_v40",
    "plan_id": "premarket_perp_capture_20260822_v40",
    "plan_hash": "fbc4456333a2d7886fac3f887d7cca1258dec5091d9671af17ccdddf42eb6c2f",
    "plan_file_sha256": "e60cc27bcaaaff01576026e8649b3be8aca38a5e9827286001d79dbd5ec9498e",
}

# Every plan this project ever published, in order. They stay on disk and are
# verified: a lineage that can silently lose a version is not a lineage. v1 and v2
# predate the versioned-filename rule and are preserved exactly as they were
# published rather than regenerated into a tidier shape.
RETIRED_PLANS = (
    {
        "schema": "premarket_perp_capture_planonly_v1",
        "plan_id": "premarket_perp_capture_20260822",
        "plan_hash": "aa174438bf457e3a57d94e8f3839ae9a61dbb42504d03f5876825f59a9b2d6c1",
        "plan_file_sha256": "cac4d34cbc6228fd0a7fc7922afb8ce3b1110388a1df860dba5bbd9f40ae2934",
        "path": "docs/plans/premarket-perp-capture-planonly-20260822.json"
    },
    {
        "schema": "premarket_perp_capture_planonly_v1",
        "plan_id": "premarket_perp_capture_20260822",
        "plan_hash": "6b4093be300c456794413486879a9302af12e86c3bf0994bfa075f7c7270592a",
        "plan_file_sha256": "22a31cd3e283e492f062e66d0f6353e9c08d336fa1ceddb2a33d0888440e8836",
        "path": "docs/plans/premarket-perp-capture-planonly-20260822-v2.json"
    },
    {
        "schema": "premarket_perp_capture_planonly_v1",
        "plan_id": "premarket_perp_capture_20260822_v3",
        "plan_hash": "ef17f97b00faf1de53eecb16b3bd4355bfabd70fd887e1df0efd787149cdef92",
        "plan_file_sha256": "60e2c64048091ea191ba40a60e69ba4916af2a13f22dcb0c089fca614d114192",
        "path": "docs/plans/premarket-perp-capture-planonly-20260822-v3.json"
    },
    {
        "schema": "premarket_perp_capture_planonly_v4",
        "plan_id": "premarket_perp_capture_20260822_v4",
        "plan_hash": "fae208baf126163e2041fccffe4c1b656848a80647b1a15b0bc0af5901dd3314",
        "plan_file_sha256": "48e8e33171425cff1642b1b9088dd24593edd5649211e3552d958933a42a4f27",
        "path": "docs/plans/premarket-perp-capture-planonly-20260822-v4.json"
    },
    {
        "schema": "premarket_perp_capture_planonly_v5",
        "plan_id": "premarket_perp_capture_20260822_v5",
        "plan_hash": "01b60cf10d82ccd523a43dc96539bce035fda73454c93b702250746b8b10d9e0",
        "plan_file_sha256": "948f8820e52b16ac3804445e830629003b33d8194f17cec46355c66e5213c349",
        "path": "docs/plans/premarket-perp-capture-planonly-20260822-v5.json"
    },
    {
        "schema": "premarket_perp_capture_planonly_v6",
        "plan_id": "premarket_perp_capture_20260822_v6",
        "plan_hash": "b2e07bd3475b57b4d815bf1adca8dbd5b52f120d4b544ea10d3227186682ab2e",
        "plan_file_sha256": "0be95c2a4a60e6457697bfa0bf612ada7b0e63efdd903abafb7ab9c77f1bbe6f",
        "path": "docs/plans/premarket-perp-capture-planonly-20260822-v6.json"
    },
    {
        "schema": "premarket_perp_capture_planonly_v7",
        "plan_id": "premarket_perp_capture_20260822_v7",
        "plan_hash": "0fb59db93f3f52a47614e080e04d59b77fbdbbc990da888b291b4cc832330e59",
        "plan_file_sha256": "6ac94a64be7a83835b764115d1805f05d2194ac060c4b4df7ddfb768bb5ab75e",
        "path": "docs/plans/premarket-perp-capture-planonly-20260822-v7.json"
    },
    {
        "schema": "premarket_perp_capture_planonly_v8",
        "plan_id": "premarket_perp_capture_20260822_v8",
        "plan_hash": "fb9a44f17ca2f3ffcb8f9ef87c7e9ad42684bfd80ad03dfe5ad48d05f34d223f",
        "plan_file_sha256": "045c614865cc0744025b93eb1ee5ef1de2d093d8680a1a9d3f2e64909839ced5",
        "path": "docs/plans/premarket-perp-capture-planonly-20260822-v8.json"
    },
    {
        "schema": "premarket_perp_capture_planonly_v9",
        "plan_id": "premarket_perp_capture_20260822_v9",
        "plan_hash": "513ecd6667fc2b5c1a1e66e5e8c9855f9cdb5a6404714b963cdb5ea0ec634296",
        "plan_file_sha256": "6b6c88868ad49e73f557dbe47c56305222174e67142e5214eaf4120229f5a098",
        "path": "docs/plans/premarket-perp-capture-planonly-20260822-v9.json"
    },
    {
        "schema": "premarket_perp_capture_planonly_v10",
        "plan_id": "premarket_perp_capture_20260822_v10",
        "plan_hash": "44c9b38829fa92841f240158dbd91677d5c8332f709905cc19ca6e215fbb0b8c",
        "plan_file_sha256": "44c317ce3e7d2cf1fd8d6a6723d62e8ee7e96564aa7db23eb0daea995ae565e3",
        "path": "docs/plans/premarket-perp-capture-planonly-20260822-v10.json"
    },
    {
        "schema": "premarket_perp_capture_planonly_v11",
        "plan_id": "premarket_perp_capture_20260822_v11",
        "plan_hash": "d383da6b870ede8009f084b6de71c6aa50d6582f0356a9ce7625bfc0685bf50f",
        "plan_file_sha256": "84f38205f7c7372d21154f527ac1db0a863e1a8e9516960220f77348f8a47792",
        "path": "docs/plans/premarket-perp-capture-planonly-20260822-v11.json"
    },
    {
        "schema": "premarket_perp_capture_planonly_v12",
        "plan_id": "premarket_perp_capture_20260822_v12",
        "plan_hash": "2bde6e7e9216bde3bbf4baa2ca9acc46b752a6589d485a0284b48873860ebf70",
        "plan_file_sha256": "3b4a6dd3ad2abdacd71276a8b8d6d77a9c409c6660cc5784fa15646df0af8033",
        "path": "docs/plans/premarket-perp-capture-planonly-20260822-v12.json"
    },
    {
        "schema": "premarket_perp_capture_planonly_v13",
        "plan_id": "premarket_perp_capture_20260822_v13",
        "plan_hash": "aa06a8e81be185f5575eaa1c0e76b93541e4b9dbd76684b7d119e2b0e9004e0f",
        "plan_file_sha256": "0593865444d2f6ff1432fc5b08d81d2b5bd9331320504b9d6aec18a990ba36d7",
        "path": "docs/plans/premarket-perp-capture-planonly-20260822-v13.json"
    },
    {
        "schema": "premarket_perp_capture_planonly_v14",
        "plan_id": "premarket_perp_capture_20260822_v14",
        "plan_hash": "b5b8b81facfbe3451b245f2d09f651215daee023190e202cba9b9670ab002a71",
        "plan_file_sha256": "2cec56843d99147cd65773b434e50257a9a82453f9656e05693a15d2681ddb1a",
        "path": "docs/plans/premarket-perp-capture-planonly-20260822-v14.json"
    },
    {
        "schema": "premarket_perp_capture_planonly_v15",
        "plan_id": "premarket_perp_capture_20260822_v15",
        "plan_hash": "41accb18028f6ccbee59264c8564d36ff9efd3d2c28aea9119e3a2d2741a062c",
        "plan_file_sha256": "47a0f340b9352b3bb9897a44e1d278f8d96e2a4d33ce5eb3339821f7f3d3bdd6",
        "path": "docs/plans/premarket-perp-capture-planonly-20260822-v15.json"
    },
    {
        "schema": "premarket_perp_capture_planonly_v16",
        "plan_id": "premarket_perp_capture_20260822_v16",
        "plan_hash": "98cbca5522753bd511ca348f2fba60134bfb8a45ed3cb96607b0b5aadb42cd4a",
        "plan_file_sha256": "5efd17a44bf307e9e90d6a581c515d07761c6e62644ce4e1ceae6b09d5246e48",
        "path": "docs/plans/premarket-perp-capture-planonly-20260822-v16.json"
    },
    {
        "schema": "premarket_perp_capture_planonly_v17",
        "plan_id": "premarket_perp_capture_20260822_v17",
        "plan_hash": "56cc373e25d1710e2fbd6fe5ac039ecb1065dfb1fbe0ead53757ae6342fb731b",
        "plan_file_sha256": "748f116c785aa5a9cc694be394eb355e554eceef7cd44a32d746cf673c406209",
        "path": "docs/plans/premarket-perp-capture-planonly-20260822-v17.json"
    },
    {
        "schema": "premarket_perp_capture_planonly_v18",
        "plan_id": "premarket_perp_capture_20260822_v18",
        "plan_hash": "dab5a0879a54aa3a6d67e36ccb43d89174d66566f4c83f2cbfb7314cabf2a93c",
        "plan_file_sha256": "0ee06a60303951cce5e1e1c7de6e9f8f9ecd636be702bf80149dcffceeac5bf0",
        "path": "docs/plans/premarket-perp-capture-planonly-20260822-v18.json"
    },
    {
        "schema": "premarket_perp_capture_planonly_v19",
        "plan_id": "premarket_perp_capture_20260822_v19",
        "plan_hash": "0e8336b3e5b85a87b21443b15af56768226833cae03000e5d03b684c306b6b11",
        "plan_file_sha256": "d5e508908154733de395b14a9530e6c69d00480d656b229959ca2f5bcb9f99f2",
        "path": "docs/plans/premarket-perp-capture-planonly-20260822-v19.json"
    },
    {
        "schema": "premarket_perp_capture_planonly_v20",
        "plan_id": "premarket_perp_capture_20260822_v20",
        "plan_hash": "25be0f87b96f0faf117eedc96f9079f8c7a5256302e714b1148f24dda0f82942",
        "plan_file_sha256": "0384f458d2642456ce3a1e94e2cb9b8300368c7e13a12a4a0763064ff0fea4ca",
        "path": "docs/plans/premarket-perp-capture-planonly-20260822-v20.json"
    },
    {
        "schema": "premarket_perp_capture_planonly_v21",
        "plan_id": "premarket_perp_capture_20260822_v21",
        "plan_hash": "0de09983d84c73a6a1c32ba8e8bea7c5c88cb22df6e8860cc82b3199d6f88f69",
        "plan_file_sha256": "997bdc7b7d5d377858e672a6da3fe01e6c562e9a16ce8612594ea4aaa12dfcf7",
        "path": "docs/plans/premarket-perp-capture-planonly-20260822-v21.json"
    },
    {
        "schema": "premarket_perp_capture_planonly_v22",
        "plan_id": "premarket_perp_capture_20260822_v22",
        "plan_hash": "08b98bbe73a6efb2c25eca08b155bc1f865c93b9e1a494ac01485c5f1f0b982d",
        "plan_file_sha256": "f79a03f26fb4e7b8d6ad1b9293bad499001ca123674358fa001a8516c94005a3",
        "path": "docs/plans/premarket-perp-capture-planonly-20260822-v22.json"
    },
    {
        "schema": "premarket_perp_capture_planonly_v23",
        "plan_id": "premarket_perp_capture_20260822_v23",
        "plan_hash": "70234ef98c2567ab5d55621c5b8f91937bfc5b1cb7b8b7142d333bbd09adf004",
        "plan_file_sha256": "a2940302e02af8cbcbee28121c8a1524880c98fc7331caca0ceeea3ec5d7a8e3",
        "path": "docs/plans/premarket-perp-capture-planonly-20260822-v23.json"
    },
    {
        "schema": "premarket_perp_capture_planonly_v24",
        "plan_id": "premarket_perp_capture_20260822_v24",
        "plan_hash": "6359c390ad8ab74a18ab27d24c5f99ee89d059e1a93693e65a6803cdfd07dbc0",
        "plan_file_sha256": "ccc69521a39aebf8794a3642e1fa4574d95285e6a41ad80ccd7dcd136f961a6f",
        "path": "docs/plans/premarket-perp-capture-planonly-20260822-v24.json"
    },
    {
        "schema": "premarket_perp_capture_planonly_v25",
        "plan_id": "premarket_perp_capture_20260822_v25",
        "plan_hash": "c5a5c663a2f5502fb4686ad906477c0437b9193424a7e67713e80e4d820b4389",
        "plan_file_sha256": "d670ad7a63fa13c9bb51b3bcad81ccd5fddb2d5439289a417f269cd90863ff5a",
        "path": "docs/plans/premarket-perp-capture-planonly-20260822-v25.json"
    },
    {
        "schema": "premarket_perp_capture_planonly_v26",
        "plan_id": "premarket_perp_capture_20260822_v26",
        "plan_hash": "ed1b5e1d4c5afbc03269905f75a01def09d31afbb5bd87dc387681747afab541",
        "plan_file_sha256": "d8a6f89a0af2e0c1c87dc7ae9efcc9b4159eba1738d4e8396d05103f7104dd64",
        "path": "docs/plans/premarket-perp-capture-planonly-20260822-v26.json"
    },
    {
        "schema": "premarket_perp_capture_planonly_v27",
        "plan_id": "premarket_perp_capture_20260822_v27",
        "plan_hash": "859bd59a406dd97ae0fb1e8239f5f34541a50cb08cbb39fbda4d189c5d7b2446",
        "plan_file_sha256": "de5c2bd1998bebd7cedd7ed728aa992ef4515ccbab4248a1d6aa8a63d644bfac",
        "path": "docs/plans/premarket-perp-capture-planonly-20260822-v27.json"
    },
    {
        "schema": "premarket_perp_capture_planonly_v28",
        "plan_id": "premarket_perp_capture_20260822_v28",
        "plan_hash": "141ab762953a21985eb6678c3c4bafb6247eadf7bef1073cc9626ee89d404d80",
        "plan_file_sha256": "b59162ee152bf1fc2301921925267731ba1c1f2f9c3d92fe4093354d55797d92",
        "path": "docs/plans/premarket-perp-capture-planonly-20260822-v28.json"
    },
    {
        "schema": "premarket_perp_capture_planonly_v29",
        "plan_id": "premarket_perp_capture_20260822_v29",
        "plan_hash": "63f4173a4d3662e6eed15f9ba1f372c8771f635b84291ed2439e076d6975a8d5",
        "plan_file_sha256": "7c93aebec952ec1d52def42ce5ac4165b6b3c8c608436ed702f50dbfb012b822",
        "path": "docs/plans/premarket-perp-capture-planonly-20260822-v29.json"
    },
    {
        "schema": "premarket_perp_capture_planonly_v30",
        "plan_id": "premarket_perp_capture_20260822_v30",
        "plan_hash": "32877c7c731bdf63167b20827f373726e34e1fbc1bcd61db26d6975444067ab5",
        "plan_file_sha256": "d68bf90c354063622a33e762a58ec610594af1bcc7359cf8125a28e9f933a192",
        "path": "docs/plans/premarket-perp-capture-planonly-20260822-v30.json"
    },
    {
        "schema": "premarket_perp_capture_planonly_v31",
        "plan_id": "premarket_perp_capture_20260822_v31",
        "plan_hash": "0359596666d918145af2fe3e172cd9907b9f286b0d25a986671be8113415bb98",
        "plan_file_sha256": "a92c2f8a105a6b63c8747d5a45e75ec22af445da286292a4666bc02d02db26b4",
        "path": "docs/plans/premarket-perp-capture-planonly-20260822-v31.json"
    },
    {
        "schema": "premarket_perp_capture_planonly_v32",
        "plan_id": "premarket_perp_capture_20260822_v32",
        "plan_hash": "15b84b04cf004834909950846837df9ccef29bb8209e56d7ca58a2b1419e784d",
        "plan_file_sha256": "d0c4a3625ff9a526166694db67d329e3d1e650fdfcbf4c60e36553983175018d",
        "path": "docs/plans/premarket-perp-capture-planonly-20260822-v32.json"
    },
    {
        "schema": "premarket_perp_capture_planonly_v33",
        "plan_id": "premarket_perp_capture_20260822_v33",
        "plan_hash": "9db73dc2e15ec266472d0cf0693f5db935f26e6b6dd3633885214e8cc965980e",
        "plan_file_sha256": "55bab73391016340ef07d705503e13ab5cd94f5677555c5dda3b6b85766ea89d",
        "path": "docs/plans/premarket-perp-capture-planonly-20260822-v33.json"
    },
    {
        "schema": "premarket_perp_capture_planonly_v34",
        "plan_id": "premarket_perp_capture_20260822_v34",
        "plan_hash": "3b307046db3c697e330c48f31c339d296e86c5d949b672025aef92976b9020ea",
        "plan_file_sha256": "79b2d256c1cd4838877274839e0024fdf415a7e0f2ab1b419ffa2ba9a6015146",
        "path": "docs/plans/premarket-perp-capture-planonly-20260822-v34.json"
    },
    {
        "schema": "premarket_perp_capture_planonly_v35",
        "plan_id": "premarket_perp_capture_20260822_v35",
        "plan_hash": "51956bf5e041f4df2424f1647c52bde438232b3f2e9303de3456e7fa98dd2950",
        "plan_file_sha256": "a82b8e52415747b4b6601af1e02efff6dd27da3f0fc4060cf7547ff15694b9e4",
        "path": "docs/plans/premarket-perp-capture-planonly-20260822-v35.json"
    },
    {
        "schema": "premarket_perp_capture_planonly_v36",
        "plan_id": "premarket_perp_capture_20260822_v36",
        "plan_hash": "5a8a2a870a387f15dfbc47e2561832d2dc7b9e8c1ab3a814065e47fef35e462c",
        "plan_file_sha256": "330ae7bbd1eeab75651d7766a61105448c96d6022c91ab73a7593dfabf088602",
        "path": "docs/plans/premarket-perp-capture-planonly-20260822-v36.json"
    },
    {
        "schema": "premarket_perp_capture_planonly_v37",
        "plan_id": "premarket_perp_capture_20260822_v37",
        "plan_hash": "9671b54040a3eabb21a4a5a3bf455ed5837fe13d82f51b5ca22dc5bbc6f6ffcf",
        "plan_file_sha256": "b8519c77cf352033c683908d011e470f94b38437dd2dfe9b8ddea297b5fd2463",
        "path": "docs/plans/premarket-perp-capture-planonly-20260822-v37.json"
    },
    {
        "schema": "premarket_perp_capture_planonly_v38",
        "plan_id": "premarket_perp_capture_20260822_v38",
        "plan_hash": "f2132603e3a3e8403a20cbceeb273cbb9f8c6804b7e5d81aaacac3b97626095b",
        "plan_file_sha256": "f7cb1f354c47ecb3b948cbd32c58ec97ba61dcebb22bab73ced8a08d26a0aabd",
        "path": "docs/plans/premarket-perp-capture-planonly-20260822-v38.json"
    },
    {
        "schema": "premarket_perp_capture_planonly_v39",
        "plan_id": "premarket_perp_capture_20260822_v39",
        "plan_hash": "d3e410f550ccf84c985924120c9970be28c87e3c788dba00ba55cde112406512",
        "plan_file_sha256": "4e010bc581bd5a2e6fd53e6a44bccc21eb3114d491eb9dc5f18a236d9a52696f",
        "path": "docs/plans/premarket-perp-capture-planonly-20260822-v39.json"
    },
)

# Compatibility aliases keep the verifier's trust-root surface deliberately tiny.
PLAN_SCHEMA = ACTIVE_PLAN["schema"]
PLAN_ID = ACTIVE_PLAN["plan_id"]
PLAN_HASH = ACTIVE_PLAN["plan_hash"]
PLAN_FILE_SHA256 = ACTIVE_PLAN["plan_file_sha256"]
