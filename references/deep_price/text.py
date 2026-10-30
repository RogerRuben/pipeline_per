import pickle


model_paths = {
    "lasso": "models/lasso.pkl",
    "ridge": "models/ridge.pkl",
    "gbdt": "models/gbdt.pkl",
    "et": "models/et.pkl",
    "svr": "models/svr.pkl"
}


for name,path in model_paths.items():

    with open(path,"rb") as f:
        model=pickle.load(f)

    print("\n======",name,"======")

    if hasattr(model,"intercept_"):
        print(
            "intercept:",
            model.intercept_
        )

    if hasattr(model,"n_features_in_"):
        print(
            "feature数:",
            model.n_features_in_
        )
import pickle
import numpy as np
import pandas as pd


# =========================
# 1. 模型路径
# =========================

model_paths = {

    "lasso": "models/lasso.pkl",
    "ridge": "models/ridge.pkl",
    "rf": "models/rf.pkl",
    "et": "models/et.pkl",
    "svr": "models/svr.pkl",
    "gbdt": "models/gbdt.pkl"

}

# 读取测试数据
test_df = pd.read_csv(
    "样本1.csv"
)


y_name = "价格_万元"   # 改成你的价格列名


X_test = test_df.loc[
    :,
    test_df.columns[1]:
].drop(
    columns=[y_name]
)


# 保证顺序和训练一致
with open(
    "feature_names.pkl",
    "rb"
) as f:
    feature_names = pickle.load(f)


X_test = X_test[
    feature_names
]


test_sample = X_test.iloc[[0]].copy()
# =========================
# 2. 测试输入
# =========================



print("特征数量:")
print(len(feature_names))

print(feature_names)


# 这里读取一条真实测试数据
# 推荐不要手动输入，直接使用训练数据中的一行

# 例如：
# X_test 是你的测试集

test_sample = X_test.iloc[[0]].copy()


print("\n测试样本维度:")
print(test_sample.shape)



# =========================
# 3. 逐个加载模型测试
# =========================

results = {}


for name,path in model_paths.items():

    print("\n======================")
    print("测试模型:",name)
    print("======================")


    try:

        with open(path,"rb") as f:
            model=pickle.load(f)


        print(
            "模型类型:",
            type(model)
        )


        # 预测

        pred = model.predict(
            test_sample
        )


        pred=float(pred[0])


        print(
            "原始predict输出:",
            pred
        )


        print(
            "exp之后:",
            np.exp(pred)
        )


        results[name]=pred



    except Exception as e:

        print(
            "模型错误:",
            e
        )



# =========================
# 4. 汇总
# =========================

print("\n\n======模型输出汇总======")

for k,v in results.items():

    print(
        f"{k:<10}",
        v
    )
