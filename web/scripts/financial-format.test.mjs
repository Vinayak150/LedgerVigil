import {test} from 'node:test';
import assert from 'node:assert/strict';
import {formatAmount,periodRange} from '../src/lib/money-format.ts';
test('exact grouping preserves decimal strings beyond JS safe integer range',()=>{
 assert.equal(formatAmount('9999999999999999.99','INR'),'9,99,99,99,99,99,99,999.99');
 assert.equal(formatAmount('9999999999999999.99','USD'),'9,999,999,999,999,999.99');
 assert.equal(formatAmount('-1234567.001','INR'),'−12,34,567.001');
 assert.equal(formatAmount('0.0000','CLF'),'0.0000');
 assert.equal(formatAmount('1e9','INR'),'Unavailable');
});
test('exclusive period end handles month and leap year boundaries in UTC',()=>{
 assert.equal(periodRange('2026-10-01','2026-11-01'),'1 Oct 2026 – 31 Oct 2026');
 assert.equal(periodRange('2024-02-01','2024-03-01'),'1 Feb 2024 – 29 Feb 2024');
});
