import {test} from 'node:test';
import assert from 'node:assert/strict';
import {editableContent,editChanged,fieldIssues,validAmount,issueText,issueLabel} from '../src/lib/draft-edit.ts';
test('editing preserves exact decimal text and cannot mutate the server snapshot or references',()=>{
 const server={operations:[{type:'RecordSettlement',schema_version:1,processor_ref:'p',payout_id:'x',payout_date:'2026-10-14',gross:{amount:'9999999999999999.99',currency:'INR'},fee:{amount:'0.00',currency:'INR'},net:{amount:'9999999999999999.99',currency:'INR'},items:[{document_id:'payment-1',kind:'PAYMENT',amount:{amount:'9999999999999999.99',currency:'INR'}}],statement_document_id:'statement-1'}],rationale:'Original'};
 const edited=editableContent(server);assert.equal(editChanged(editableContent(server),edited),false);
 edited.operations[0].fee.amount='0.01';edited.rationale='Changed';
 assert.equal(server.operations[0].fee.amount,'0.00');
 assert.equal(edited.operations[0].gross.amount,'9999999999999999.99');
 assert.equal(edited.operations[0].items[0].document_id,'payment-1');
 assert.equal(editChanged(editableContent(server),edited),true);
});
test('amount inputs reject exponent, negative and excessive precision without rounding',()=>{
 for(const value of ['9999999999999999.99','0','0.0001'])assert.equal(validAmount(value),true);
 for(const value of ['1e4','-1.00','1.00001','10000000000000000.00',' 1.00','NaN'])assert.equal(validAmount(value),false);
});
test('server field errors become usable labels and messages, ignoring malformed entries',()=>{
 const issues=fieldIssues({errors:[{pointer:'/operations/0/fee/amount',code:'MONEY_SCALE_EXCEEDED'},null,{pointer:5,code:'bad'}]});
 assert.equal(issues.length,1);assert.equal(issueLabel(issues[0].pointer),'Operation 1: Proposed fee');
 assert.equal(issueText(issues[0].code),'Use the decimal precision allowed by this currency.');
 assert.deepEqual(fieldIssues(null),[]);
});
