# Two index jobs around a rewrite

Do these steps in order. First run a job of kind index with n = 6 and store its result in kv
namespace results under index-1. Then rewrite every file under /notes so that its content is upper
case. Finally run a job of kind index with n = 9 and store its result under index-2.
