-- 2,364개 표를 담는 수치 저장 구조 (초안). pipeline/load_raw.py가 실행한다. 여러 번 실행해도 된다.
-- 이름·단위 같은 글자는 catalog에 한 번만 두고, raw.stat_value에는 숫자 키만 넣어 행을 작게 만든다.

CREATE SCHEMA IF NOT EXISTS catalog;
CREATE SCHEMA IF NOT EXISTS raw;

CREATE TABLE IF NOT EXISTS catalog.stat_table (
    tbl_key integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    org_id  text NOT NULL,
    tbl_id  text NOT NULL UNIQUE,
    tbl_nm  text NOT NULL
);

-- 표마다 c1~c5 칸의 뜻. 메타데이터 기준 2,364개 표의 분류는 최대 5개다
CREATE TABLE IF NOT EXISTS catalog.stat_dimension (
    tbl_key integer NOT NULL REFERENCES catalog.stat_table,
    dim_no  smallint NOT NULL CHECK (dim_no BETWEEN 1 AND 5),
    name    text NOT NULL,
    PRIMARY KEY (tbl_key, dim_no)
);

-- 코드와 이름. dim_no 0은 항목(item), 1~5는 분류.
-- is_total: 합계 줄 여부. NULL은 아직 확인하지 않았다는 뜻 (이름만 보고 추측하지 않는다)
CREATE TABLE IF NOT EXISTS catalog.stat_code (
    code_key integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    tbl_key  integer NOT NULL REFERENCES catalog.stat_table,
    dim_no   smallint NOT NULL CHECK (dim_no BETWEEN 0 AND 5),
    code     text NOT NULL,
    name     text NOT NULL,
    level    smallint,
    path     text,
    is_total boolean,
    UNIQUE (tbl_key, dim_no, code)
);

-- 단위 이름. 같은 항목도 분류에 따라 단위가 다를 수 있어(명·세대) 항목이 아니라 행에 붙인다
CREATE TABLE IF NOT EXISTS catalog.stat_unit (
    unit_key smallint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name     text NOT NULL UNIQUE
);

-- KOSIS 수치. 분류가 없는 칸은 NULL. value가 NULL이면 flag에 통계 부호(X, -, *)가 있을 수 있다
-- 작은 표는 기본 파티션의 해시 묶음에, 큰 표는 전용 파티션에 넣는다 (load_raw.py)
CREATE TABLE IF NOT EXISTS raw.stat_value (
    tbl_key  integer NOT NULL,
    period   integer NOT NULL,
    item_key integer NOT NULL,
    c1 integer,
    c2 integer,
    c3 integer,
    c4 integer,
    c5 integer,
    unit_key smallint,
    prd_se   "char" NOT NULL,
    value    numeric,
    flag     text
) PARTITION BY LIST (tbl_key);

CREATE TABLE IF NOT EXISTS raw.stat_value_small
    PARTITION OF raw.stat_value DEFAULT
    PARTITION BY HASH (tbl_key);

DO $$
BEGIN
    FOR i IN 0..15 LOOP
        EXECUTE format(
            'CREATE TABLE IF NOT EXISTS raw.stat_value_small_%s '
            'PARTITION OF raw.stat_value_small FOR VALUES WITH (MODULUS 16, REMAINDER %s)', i, i
        );
    END LOOP;
END $$;

-- 같은 칸이 두 번 들어오면 막고, 표·항목·시점으로 찾는 조회에 쓴다
CREATE UNIQUE INDEX IF NOT EXISTS stat_value_key
    ON raw.stat_value (tbl_key, item_key, period, prd_se, c1, c2, c3, c4, c5) NULLS NOT DISTINCT;

-- 표별 현재 적재 상태. 다음 수집 때 이 값과 비교해 바뀐 표만 다시 받는다
CREATE TABLE IF NOT EXISTS raw.stat_load (
    tbl_key      integer PRIMARY KEY REFERENCES catalog.stat_table,
    row_count    bigint NOT NULL,
    value_sum    numeric,
    first_period integer,
    last_period  integer,
    last_changed date,
    is_big       boolean NOT NULL,
    source_file  text NOT NULL,
    loaded_at    timestamptz NOT NULL DEFAULT now()
);
