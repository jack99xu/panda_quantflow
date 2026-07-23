from typing import Optional, Type

from panda_plugins.base import BaseWorkNode, work_node, ui
from pydantic import BaseModel, Field, field_validator
import pandas as pd
from panda_backtest.main_workflow_stock import start, get_backtest_id


@ui(
    factors={"input_type": "None"},
    code={"input_type": "text_field", "placeholder": "策略代码"},
    frequency={"input_type": "combobox", "options": ["1d", ], "placeholder": "回测频率", "allow_link": False},
    start_capital={"input_type": "number_field", "placeholder": "请输入初始资金", "allow_link": False},
    standard_symbol={"input_type": "combobox", "options": ["上证指数", "沪深300", "中证500", "中证1000"]},
    commission_rate={"input_type": "number_field", "placeholder": "请输入佣金率", "allow_link": False},
    start_date={"input_type":"date_field","placeholder": "开始日期", "allow_link": False},
    end_date={"input_type":"date_field","placeholder": "结束日期", "allow_link": False}
)

class StockBacktestInputModel(BaseModel):
    code:str = Field(default="",title="策略代码")
    factors: pd.DataFrame = Field(default_factory=pd.DataFrame, title="因子值")
    start_capital: int = Field(default=10000000, title="初始资金")
    standard_symbol: str = Field(default="上证指数", title="基准指数")
    commission_rate: int = Field(default=1, title="佣金率")
    frequency:str=Field(default="1d",title="回测频率")
    start_date: str = Field(default="20241001", title="回测开始时间")
    end_date: str = Field(default="20241231", title="回测结束时间")
    model_config = {"arbitrary_types_allowed": True}

    @field_validator('factors')
    def validate_df_factor(cls, v):
        if not isinstance(v, pd.DataFrame):
            raise ValueError('factors must be a pandas DataFrame')
        return v


class StockBacktestOutputModel(BaseModel):
    backtest_id: str = Field(default="error", title="回测id")


@work_node(name="股票回测", group="05-回测相关", type="general", box_color="yellow")
class StockBacktestControl(BaseWorkNode):
    # Return the input model
    # 返回输入模型
    @classmethod
    def input_model(cls) -> Optional[Type[BaseModel]]:
        return StockBacktestInputModel

    # Return the output model
    # 返回输出模型
    @classmethod
    def output_model(cls) -> Optional[Type[BaseModel]]:
        return StockBacktestOutputModel

    # Node running logic
    # 节点运行逻辑
    def run(self, input: BaseModel) -> BaseModel:
        print("StockBacktestControl")
        back_test_id = get_backtest_id()
        try:
            start(
                back_test_id=back_test_id,
                code=input.code,
                start_date=input.start_date,
                end_date=input.end_date,
                frequency=input.frequency,
                start_capital=input.start_capital,
                standard_symbol=input.standard_symbol,
                commission_rate=input.commission_rate,
                account_id="8888",
                df_factor=input.factors,
            )
        except Exception as e:
            error_msg = str(getattr(e, 'message', e))
            self.log_error(f"回测执行失败: {error_msg}")
            return StockBacktestOutputModel(backtest_id=str(back_test_id))
        return StockBacktestOutputModel(backtest_id=str(back_test_id))


if __name__ == "__main__":
    node = StockBacktestControl()
    df = pd.read_csv(
        '/Users/peiqi/code/python/panda_factor/panda_factor/test/1.csv',
        usecols=["date", "symbol", "bs4177e23"],  # 只读取需要的列，节省内存
        dtype={"date": str}  # 明确指定date列为字符串类型
    )
    input = StockBacktestInputModel(df_factor=df)
    print(node.run(input))
